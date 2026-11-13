"""
Ledger signer — the erasure signer is not the erasure caller (ADR-0020, C4).

A chained, signed receipt log proves that entries were not altered or removed after they
were written. It cannot prove that the process which wrote them told the truth: when the
same process holds the signing key, the log's write handle, and the delete call, a buggy or
compromised caller can sign a receipt that verifies, because the signer is legitimate.

This module is the separate process. It holds (a) the receipt private key and (b) the only
write handle to its receipt log. Callers hand it a claim; it signs with ITS key, appends,
and returns the entry. The caller holds neither key nor handle.

What it verifies, honestly: for an adapter receipt it cannot see inside the caller's store,
so the `before` digest is recorded as caller-asserted and the signer attests custody
separation only (the receipt was issued by a process the caller does not control, into a
log the caller cannot write). Verification of the claim itself (reading the fact back, the
key tombstone) is the server-side signer's job, built on this same endpoint in the API
cutover PR.

Wire format
-----------
POST /v1/sign/receipt   {before, after, scope, caller?, caller_erased_at?}
  → 200 {receipt: {before, after, scope, key_id, timestamp, signature(hex)},
         entry:   {seq, prev_entry_hash, receipt, entry_hash,
                   signer, caller, caller_erased_at, recorded_at},
         public_key(hex)}
GET  /v1/sign/health    → {ok, key_id, public_key(hex), entries}
GET  /v1/sign/entries   → [entry, ...]           (read-only)
GET  /v1/sign/verify    → {ok, problems, entries}

The entry's `receipt` dict and chain hashing are byte-identical to the LangGraph adapter's
`receipt_log.py` (`_entry_hash = sha256(prev_entry_hash + canonical(receipt))`), so the
adapter's `verify_log(entries, trusted_keys)` verifies this log unchanged. Entry-level
annotations (`signer`, `caller`, `caller_erased_at`, `recorded_at`) are NOT inside the
chain hash — they are operator context, stated as such; the receipt's signed payload and
the chain are what an auditor checks.

Auth: every request carries `Authorization: Bearer <GRAFOMEM_SIGNER_TOKEN>`; compared in
constant time. The service refuses to start without a token unless UNSAFE_LOCAL_DEV=1.
"""
import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

GENESIS_HASH = "0" * 64


# ── chain primitives (identical to the adapter's receipt_log.py) ─────────────

def canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def entry_hash(prev_entry_hash: str, receipt_dict: dict) -> str:
    return hashlib.sha256((prev_entry_hash + canonical(receipt_dict)).encode("utf-8")).hexdigest()


def receipt_payload(before: str, after: str, scope: str, key_id: str, timestamp: str) -> bytes:
    """The bytes a Receipt signs — same formula as grafomem.runtime.Receipt.verify."""
    return f"{before}|{after}|{scope}|{key_id}|{timestamp}".encode("utf-8")


class ChainedJsonlLog:
    """Append-only, hash-chained JSON Lines log — the signer's write handle. One process,
    one file, one lock. On construction the file is read so a restarted signer continues
    the chain."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last_hash = GENESIS_HASH
        self._count = 0
        existing = self._read_raw()
        if existing:
            self._count = len(existing)
            self._last_hash = existing[-1]["entry_hash"]

    def _read_raw(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out: list[dict] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def append(self, receipt_dict: dict, annotations: dict | None = None) -> dict:
        with self._lock:
            entry = {
                "seq": self._count,
                "prev_entry_hash": self._last_hash,
                "receipt": receipt_dict,
                "entry_hash": entry_hash(self._last_hash, receipt_dict),
            }
            if annotations:
                entry.update(annotations)   # outside the chain hash, by design (see module doc)
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._last_hash = entry["entry_hash"]
            self._count += 1
            return entry

    def entries(self) -> list[dict]:
        with self._lock:
            return self._read_raw()


def verify_entries(entries: list[dict], public_key) -> tuple[bool, list[str]]:
    """Chain + per-entry hash + signature, with one trusted key (the signer's own)."""
    from cryptography.exceptions import InvalidSignature
    problems: list[str] = []
    prev = GENESIS_HASH
    for i, e in enumerate(entries):
        if e.get("prev_entry_hash") != prev:
            problems.append(f"entry {i}: broken chain (prev_entry_hash mismatch)")
        rd = e.get("receipt", {})
        if entry_hash(e.get("prev_entry_hash", ""), rd) != e.get("entry_hash"):
            problems.append(f"entry {i}: entry_hash does not match contents (tampered)")
        try:
            public_key.verify(bytes.fromhex(rd["signature"]),
                              receipt_payload(rd["before"], rd["after"], rd["scope"], rd["key_id"], rd["timestamp"]))
        except (InvalidSignature, KeyError, ValueError) as ex:
            problems.append(f"entry {i}: receipt signature does not verify ({type(ex).__name__})")
        prev = e.get("entry_hash", prev)
    return (not problems, problems)


# ── the signer ───────────────────────────────────────────────────────────────

class LedgerSigner:
    """Holds the receipt key and the log. `sign_receipt` is the only way an entry is born."""

    def __init__(self, private_key, key_id: str, log: ChainedJsonlLog) -> None:
        self._priv = private_key
        self.key_id = key_id
        self._log = log

    @classmethod
    def from_seed_hex(cls, seed_hex: str, key_id: str, log: ChainedJsonlLog) -> "LedgerSigner":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        seed = bytes.fromhex(seed_hex)
        if len(seed) != 32:
            raise ValueError("GRAFOMEM_SIGNER_KEY must be a 32-byte hex seed")
        return cls(Ed25519PrivateKey.from_private_bytes(seed), key_id, log)

    def public_key_hex(self) -> str:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return self._priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def public_key(self):
        return self._priv.public_key()

    def sign_receipt(self, *, before: str, after: str, scope: str,
                     caller: str | None = None, caller_erased_at: str | None = None) -> dict:
        """Sign and append. The timestamp is the SIGNER's clock (`recorded_at`); what the
        caller says about when it erased travels as an annotation, visibly separate."""
        for name, val in (("before", before), ("after", after), ("scope", scope)):
            if not isinstance(val, str) or not val:
                raise ValueError(f"{name} must be a non-empty string")
        timestamp = datetime.now(timezone.utc).isoformat()
        sig = self._priv.sign(receipt_payload(before, after, scope, self.key_id, timestamp))
        rd = {"before": before, "after": after, "scope": scope, "key_id": self.key_id,
              "timestamp": timestamp, "signature": sig.hex()}
        entry = self._log.append(rd, {
            "signer": "grafomem-ledger-signer",
            "caller": caller,
            "caller_erased_at": caller_erased_at,
            "recorded_at": timestamp,
            "before_basis": "caller_asserted",   # this signer cannot read the caller's store
        })
        return {"receipt": rd, "entry": entry, "public_key": self.public_key_hex()}

    def entries(self) -> list[dict]:
        return self._log.entries()

    def verify(self) -> tuple[bool, list[str]]:
        return verify_entries(self._log.entries(), self._priv.public_key())


# ── HTTP surface ─────────────────────────────────────────────────────────────

def create_signer_app(signer: LedgerSigner, token: str | None):
    from fastapi import FastAPI, HTTPException, Request
    from pydantic import BaseModel

    if not token and not os.environ.get("UNSAFE_LOCAL_DEV"):
        raise RuntimeError("GRAFOMEM_SIGNER_TOKEN is required (or UNSAFE_LOCAL_DEV=1 for local tests)")

    app = FastAPI(title="GRAFOMEM ledger signer", version="0.1.0", docs_url=None, redoc_url=None)

    def _auth(request: Request) -> None:
        if not token:
            return
        hdr = request.headers.get("authorization", "")
        got = hdr[7:] if hdr.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(got.encode(), token.encode()):
            raise HTTPException(401, "signer: bad or missing bearer token")

    class SignReceiptRequest(BaseModel):
        before: str
        after: str
        scope: str
        caller: str | None = None
        caller_erased_at: str | None = None

    @app.post("/v1/sign/receipt")
    def sign_receipt(req: SignReceiptRequest, request: Request) -> dict:
        _auth(request)
        try:
            return signer.sign_receipt(before=req.before, after=req.after, scope=req.scope,
                                       caller=req.caller, caller_erased_at=req.caller_erased_at)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/v1/sign/health")
    def health(request: Request) -> Any:
        _auth(request)
        return {"ok": True, "key_id": signer.key_id, "public_key": signer.public_key_hex(),
                "entries": len(signer.entries())}

    @app.get("/v1/sign/entries")
    def entries(request: Request) -> Any:
        _auth(request)
        return signer.entries()

    @app.get("/v1/sign/verify")
    def verify(request: Request) -> Any:
        _auth(request)
        ok, problems = signer.verify()
        return {"ok": ok, "problems": problems, "entries": len(signer.entries())}

    return app


def signer_from_env() -> tuple[LedgerSigner, Optional[str]]:
    """Build the signer from GRAFOMEM_SIGNER_KEY / _KEY_ID / _LOG / _TOKEN."""
    seed = os.environ.get("GRAFOMEM_SIGNER_KEY")
    if not seed:
        if os.environ.get("UNSAFE_LOCAL_DEV"):
            seed = os.urandom(32).hex()
        else:
            raise RuntimeError("GRAFOMEM_SIGNER_KEY (32-byte hex seed) is required")
    key_id = os.environ.get("GRAFOMEM_SIGNER_KEY_ID", "grafomem_ledger_signer")
    log = ChainedJsonlLog(os.environ.get("GRAFOMEM_SIGNER_LOG", "grafomem_signer_log.jsonl"))
    return LedgerSigner.from_seed_hex(seed, key_id, log), os.environ.get("GRAFOMEM_SIGNER_TOKEN")
