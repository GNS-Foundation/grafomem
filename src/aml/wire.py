"""
GRAFOMEM GMP v0.2 wire binding — HTTP + JSON.

The spec (GMP §0, D1) defers the wire encoding; this is it. It turns GMP from an
in-process Python Protocol (`interface.py`) into a *network* protocol: a server
that exposes any `MemoryBackend` over HTTP, and a client that itself implements the
`MemoryBackend` Protocol by proxying each call over the wire.

Design (see module header in the chat for rationale):
  - Transport: HTTP + JSON. No gRPC/protobuf — a reference binding must be
    implementable in any language with no tooling, and legible on the wire.
  - Server: stdlib `http.server`, zero new dependencies.
  - Named stores: `POST /v1/stores` creates a fresh backend and returns a
    `store_id`; every operation is scoped to one. This is the right product shape
    *and* it lets the conformance suite run remotely (a fresh store per seed).
  - Refs are opaque (anchor B5): they cross the wire as raw JSON values; the client
    stores and returns them without interpreting. (v0.1 assumes JSON-serializable
    refs — true for the reference backend's integer refs; a bytes-ref backend would
    base64 them, deferred.)
  - `GMPClient` IS a `MemoryBackend`. So `run_conformance(lambda:
    GMPClient.create(url))` against a server wrapping `GMPReferenceBackend` passes
    the full profile — the suite, unchanged, over a socket.

Endpoints (all JSON bodies):
  POST /v1/stores                         -> {"store_id": "..."}
  GET  /v1/stores/{id}/capabilities       -> {"capabilities": ["audit", ...]}
  POST /v1/stores/{id}/write              {content, options}      -> {"ref": <opaque>}
  POST /v1/stores/{id}/supersede          {old_ref, content, options} -> {"ref": <opaque>}
  POST /v1/stores/{id}/delete             {ref}                   -> {"deleted": bool}
  POST /v1/stores/{id}/retrieve           {query, options}        -> {"memories": [...]}
  GET  /v1/stores/{id}/audit              -> {"memories": [...]}
  POST /v1/stores/{id}/flush              -> {}

Errors: a `CapabilityNotSupported` becomes HTTP 422 with
{"error": "capability_not_supported", "capability": "...", "operation": "..."},
which the client re-raises as the same exception. Unknown store -> 404; malformed
-> 400; anything else -> 500 with the message.

Requires grafomem[backends] for the reference backend; the wire layer itself is
stdlib-only.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aml.backends.interface import (
    Capability,
    CapabilityNotSupported,
    Memory,
    RetrieveOptions,
    SourceMeta,
    WriteOptions,
)

API = "/v1"
BackendFactory = Callable[[], object]   # () -> a fresh MemoryBackend


# ---------------------------------------------------------------------------
# Codec — every GMP type to/from JSON-safe values
# ---------------------------------------------------------------------------

def _enc_dt(dt: datetime | None):
    return dt.isoformat() if dt is not None else None


def _dec_dt(s):
    return datetime.fromisoformat(s) if s is not None else None


def _enc_bytes(b: bytes | None):
    return base64.b64encode(b).decode("ascii") if b is not None else None


def _dec_bytes(s):
    return base64.b64decode(s) if s is not None else None


def enc_source(s: SourceMeta | None):
    if s is None:
        return None
    return {
        "write_id": s.write_id,
        "written_at": _enc_dt(s.written_at),
        "written_by": s.written_by,
        "signature": _enc_bytes(s.signature),
        "public_key": _enc_bytes(s.public_key),
    }


def dec_source(d):
    if d is None:
        return None
    return SourceMeta(
        write_id=d.get("write_id"),
        written_at=_dec_dt(d.get("written_at")),
        written_by=d.get("written_by"),
        signature=_dec_bytes(d.get("signature")),
        public_key=_dec_bytes(d.get("public_key")),
    )


def enc_memory(m: Memory):
    return {
        "ref": m.ref,                       # opaque, pass-through (B5)
        "content": m.content,
        "written_at": _enc_dt(m.written_at),
        "metadata": m.metadata,
        "valid_from": _enc_dt(m.valid_from),
        "valid_until": _enc_dt(m.valid_until),
        "tenant_id": m.tenant_id,
        "superseded_by": m.superseded_by,   # opaque ref or null
        "source": enc_source(m.source),
    }


def dec_memory(d) -> Memory:
    return Memory(
        ref=d["ref"],
        content=d["content"],
        written_at=_dec_dt(d["written_at"]),
        metadata=d.get("metadata", {}),
        valid_from=_dec_dt(d.get("valid_from")),
        valid_until=_dec_dt(d.get("valid_until")),
        tenant_id=d.get("tenant_id"),
        superseded_by=d.get("superseded_by"),
        source=dec_source(d.get("source")),
    )


def enc_write_options(o: WriteOptions):
    return {
        "valid_from": _enc_dt(o.valid_from),
        "tenant_id": o.tenant_id,
        "signing_key": _enc_bytes(o.signing_key),
        "metadata": o.metadata,
    }


def dec_write_options(d) -> WriteOptions:
    d = d or {}
    return WriteOptions(
        valid_from=_dec_dt(d.get("valid_from")),
        tenant_id=d.get("tenant_id"),
        signing_key=_dec_bytes(d.get("signing_key")),
        metadata=d.get("metadata", {}),
    )


def enc_retrieve_options(o: RetrieveOptions):
    return {
        "budget_tokens": o.budget_tokens,
        "as_of": _enc_dt(o.as_of),
        "tenant_id": o.tenant_id,
        "top_k": o.top_k,
    }


def dec_retrieve_options(d) -> RetrieveOptions:
    d = d or {}
    kwargs = {
        "as_of": _dec_dt(d.get("as_of")),
        "tenant_id": d.get("tenant_id"),
        "top_k": d.get("top_k"),
    }
    if d.get("budget_tokens") is not None:   # client always sends it; default if absent
        kwargs["budget_tokens"] = d["budget_tokens"]
    return RetrieveOptions(**kwargs)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class _GMPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, backend_factory: BackendFactory):
        super().__init__(addr, handler)
        self.backend_factory = backend_factory
        self.stores: dict[str, object] = {}
        self._lock = threading.Lock()

    def new_store(self) -> str:
        sid = uuid.uuid4().hex[:12]
        with self._lock:
            self.stores[sid] = self.backend_factory()
        return sid


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):           # silence default stderr logging
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n == 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _parts(self):
        return self.path.strip("/").split("/")

    def _store(self, sid):
        return self.server.stores.get(sid)

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        p = self._parts()
        try:
            # /v1/stores/{id}/capabilities | /audit
            if len(p) == 4 and p[0] == "v1" and p[1] == "stores":
                _, _, sid, op = p
                b = self._store(sid)
                if b is None:
                    return self._send(404, {"error": "unknown_store", "store_id": sid})
                if op == "capabilities":
                    return self._send(200, {"capabilities":
                                            sorted(c.value for c in b.capabilities())})
                if op == "audit":
                    return self._send(200, {"memories": [enc_memory(m) for m in b.audit()]})
            return self._send(404, {"error": "not_found", "path": self.path})
        except Exception as e:                              # pragma: no cover
            return self._send(500, {"error": "internal", "message": str(e)})

    def do_POST(self):
        p = self._parts()
        try:
            if p == ["v1", "stores"]:
                return self._send(200, {"store_id": self.server.new_store()})

            if len(p) == 4 and p[0] == "v1" and p[1] == "stores":
                _, _, sid, op = p
                b = self._store(sid)
                if b is None:
                    return self._send(404, {"error": "unknown_store", "store_id": sid})
                body = self._read_json()
                return self._dispatch(b, op, body)

            return self._send(404, {"error": "not_found", "path": self.path})
        except CapabilityNotSupported as e:
            return self._send(422, {"error": "capability_not_supported",
                                    "capability": e.capability.value,
                                    "operation": e.operation})
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": "bad_request", "message": str(e)})
        except Exception as e:                              # pragma: no cover
            return self._send(500, {"error": "internal", "message": str(e)})

    def _dispatch(self, b, op: str, body: dict):
        if op == "write":
            ref = b.write(body["content"], dec_write_options(body.get("options")))
            return self._send(200, {"ref": ref})
        if op == "supersede":
            ref = b.supersede(body["old_ref"], body["content"],
                              dec_write_options(body.get("options")))
            return self._send(200, {"ref": ref})
        if op == "delete":
            return self._send(200, {"deleted": bool(b.delete(body["ref"]))})
        if op == "retrieve":
            mems = b.retrieve(body["query"], dec_retrieve_options(body.get("options")))
            return self._send(200, {"memories": [enc_memory(m) for m in mems]})
        if op == "flush":
            b.flush()
            return self._send(200, {})
        return self._send(404, {"error": "unknown_op", "op": op})


def serve(backend_factory: BackendFactory, host: str = "127.0.0.1", port: int = 8731):
    """Blocking server. For tests use `start_background` instead."""
    srv = _GMPHTTPServer((host, port), _Handler, backend_factory)
    srv.serve_forever()


def start_background(backend_factory: BackendFactory, host: str = "127.0.0.1"):
    """Start a server on an ephemeral port in a daemon thread.
    Returns (server, base_url). Call server.shutdown() to stop."""
    srv = _GMPHTTPServer((host, 0), _Handler, backend_factory)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://{host}:{srv.server_address[1]}"


# ---------------------------------------------------------------------------
# Client — a MemoryBackend that speaks over HTTP
# ---------------------------------------------------------------------------

class GMPClient:
    """Implements the MemoryBackend Protocol by proxying to a GMP HTTP server."""

    __grafomem_interface__ = "0.1.1"

    def __init__(self, base_url: str, store_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.store_id = store_id

    @classmethod
    def create(cls, base_url: str) -> "GMPClient":
        """Create a fresh store on the server and return a client bound to it."""
        sid = _request(base_url.rstrip("/") + f"{API}/stores", "POST", {})["store_id"]
        return cls(base_url, sid)

    def _url(self, op: str) -> str:
        return f"{self.base_url}{API}/stores/{self.store_id}/{op}"

    def capabilities(self) -> set[Capability]:
        out = _request(self._url("capabilities"), "GET")
        return {Capability(c) for c in out["capabilities"]}

    def write(self, content: str, options: WriteOptions):
        return _request(self._url("write"), "POST",
                        {"content": content, "options": enc_write_options(options)})["ref"]

    def supersede(self, old_ref, content: str, options: WriteOptions):
        return _request(self._url("supersede"), "POST",
                        {"old_ref": old_ref, "content": content,
                         "options": enc_write_options(options)})["ref"]

    def delete(self, ref) -> bool:
        return bool(_request(self._url("delete"), "POST", {"ref": ref})["deleted"])

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        out = _request(self._url("retrieve"), "POST",
                       {"query": query, "options": enc_retrieve_options(options)})
        return [dec_memory(m) for m in out["memories"]]

    def audit(self) -> Iterator[Memory]:
        out = _request(self._url("audit"), "GET")
        return iter([dec_memory(m) for m in out["memories"]])

    def flush(self) -> None:
        _request(self._url("flush"), "POST", {})


def _request(url: str, method: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = {}
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            pass
        if e.code == 422 and payload.get("error") == "capability_not_supported":
            raise CapabilityNotSupported(
                Capability(payload["capability"]), payload["operation"]) from None
        raise RuntimeError(f"GMP server {e.code}: {payload or e.reason}") from None


# ============================================================================
# Self-validating smoke — run `python -m aml.wire`
#
# Stands up a server wrapping GMPReferenceBackend, drives every operation through
# the client over HTTP, then runs the conformance suite through the client and
# asserts the full GMP v0.2 profile. The protocol crosses a socket unchanged.
# ============================================================================

if __name__ == "__main__":
    from datetime import timedelta, timezone

    from aml.backends.interface import MemoryBackend
    from aml.backends.gmp_reference import GMP_V02_PROFILE, GMPReferenceBackend
    from aml.backends.vector_only import _stub_embedder
    from aml.eval.conformance import run_conformance

    print("GRAFOMEM wire binding — GMP v0.2 over HTTP+JSON (STUB embedder)\n")

    server, url = start_background(lambda: GMPReferenceBackend(embed_fn=_stub_embedder()))
    print(f"server up at {url}")

    c = GMPClient.create(url)
    assert isinstance(c, MemoryBackend)
    assert c.capabilities() == set(GMP_V02_PROFILE)
    print("✓ client is a MemoryBackend          (capabilities round-trip over HTTP)")

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1, t2 = t0 + timedelta(days=30), t0 + timedelta(days=60)
    r0 = c.write("Aria lives in Rome", WriteOptions(valid_from=t0, tenant_id="A"))
    r1 = c.supersede(r0, "Aria lives in Milan", WriteOptions(valid_from=t1, tenant_id="A"))
    r2 = c.supersede(r1, "Aria lives in Turin", WriteOptions(valid_from=t2, tenant_id="A"))
    c.write("Bruno lives in Naples", WriteOptions(valid_from=t0, tenant_id="B"))
    c.flush()

    now = [m.content for m in c.retrieve("Where does Aria live?",
                                         RetrieveOptions(tenant_id="A", budget_tokens=512))]
    assert now == ["Aria lives in Turin"], now
    past = [m.content for m in c.retrieve("Where does Aria live?",
            RetrieveOptions(as_of=t1 + timedelta(days=5), tenant_id="A", budget_tokens=512))]
    assert past == ["Aria lives in Milan"], past
    print("✓ versioning + as_of over the wire   (head = Turin; as_of(t1) = Milan)")

    bq = [m.content for m in c.retrieve("Where does Aria live?",
                                        RetrieveOptions(tenant_id="B", budget_tokens=512))]
    assert all("Aria" not in x for x in bq), bq
    assert c.delete(r2) is True and c.delete(r2) is False
    assert all(m.content != "Aria lives in Turin" for m in c.audit())
    print("✓ tenant isolation + honest delete   (B can't see A; delete gone from audit)")

    print("\n  running the conformance suite THROUGH the client (a few thousand HTTP "
          "calls; stub embedder)...")
    profile = run_conformance(lambda: GMPClient.create(url),
                              name="GMPClient->GMPReferenceBackend", seeds=range(2))
    print(f"  SUPPORTS {{{', '.join(sorted(x.value for x in profile.supported))}}}")
    assert profile.supported == set(GMP_V02_PROFILE), set(GMP_V02_PROFILE) - profile.supported
    assert not profile.violations, [r.capability.value for r in profile.violations]

    server.shutdown()
    print("\n✓ The conformance suite passes over HTTP — full GMP v0.2 profile, no "
          "violations.\n  The protocol crosses a socket unchanged. Wire binding green.")
