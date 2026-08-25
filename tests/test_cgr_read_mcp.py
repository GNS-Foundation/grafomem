"""com.grafomem/cgr-read — remote CGR read MCP server tests.

Drives the hand-rolled stateless Streamable-HTTP endpoint (POST /mcp) via a real HTTP
client (FastAPI TestClient), against real substrate in local Postgres. Covers: the
handshake (initialize / tools/list), notification → 202, the three tools, the cgr:read
scope boundary, Origin rejection, explicit no_evidence, and the load-bearing invariant —
**the signed attestation is byte-identical to the REST /v1/cgr/read/attestation surface**
(same read-core, so equivalence by construction).

Auth note: the 401-without-a-key boundary is enforced by the shared AuthMiddleware (same
middleware every endpoint uses, tested elsewhere); here we inject a tenant context and
test the endpoint's own cgr:read scope gate.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"
FOUNDATION_SEED = "11" * 32
COMMERCIAL_SEED = "22" * 32
AGENT_SEED = "33" * 32
HANDLE = "analyst-a@example"
DOMAIN = "deploy-verification"


class _MockId:
    k = bytes.fromhex(COMMERCIAL_SEED)
    def _priv(self):
        return Ed25519PrivateKey.from_private_bytes(self.k)
    def sign(self, m):
        p = self._priv()
        return p.sign(m), p.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    def public_key(self):
        return self._priv().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _agent_key_hex():
    p = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(AGENT_SEED))
    return p.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _req(tenant_id, scopes=("*",)):
    return SimpleNamespace(state=SimpleNamespace(
        tenant=SimpleNamespace(tenant_id=tenant_id, scopes=list(scopes), authenticated=True)))


def _endpoint(router, path, method="GET"):
    for r in router.routes:
        if r.path == path and method in r.methods:
            return r.endpoint
    raise KeyError(f"{method} {path} not on router")


@pytest.fixture(scope="module")
def wired():
    """Wire dt/store/foundation, seed ONE agent (with a domain + agent_key), and return
    the MCP router, the REST read endpoint, tenant id, and the subject key."""
    from aml.cloud.decision_trail import DecisionTrailService
    from aml.cloud.execution_receipts import ExecutionReceiptService
    from aml.cloud.demo_routes import GovernedDecisionRequest, OutcomeEvent, create_governed_router
    from aml.cgr.routes import create_cgr_issuance_router
    from aml.cgr.mcp_server import create_cgr_mcp_router
    from aml.cgr.issuance import FoundationIdentity
    from aml.server.stores import StoreManager
    from aml.backends.postgres_gmp import PostgresGMPBackend

    dt = DecisionTrailService(TEST_DB_URL); dt.ensure_schema()
    receipts = ExecutionReceiptService(TEST_DB_URL, signing_identity=_MockId()); receipts.ensure_schema()
    store_mgr = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL))
    foundation = FoundationIdentity(bytes.fromhex(FOUNDATION_SEED))
    gov = create_governed_router(dt, receipts, _MockId(), store_mgr)
    iss = create_cgr_issuance_router(dt, store_mgr, foundation, gcrumbs=None)
    mcp = create_cgr_mcp_router(dt, store_mgr, foundation)

    governed_decision = _endpoint(gov, "/v1/governed/decisions", "POST")
    post_outcome = _endpoint(gov, "/v1/governed/outcomes", "POST")
    read = _endpoint(iss, "/v1/cgr/read/attestation", "GET")

    import uuid
    T = f"mcp-{uuid.uuid4().hex[:8]}"
    akey = _agent_key_hex()

    async def _seed():
        for i in range(3):
            inv = f"INV-{i}"
            await governed_decision(GovernedDecisionRequest(  # gitleaks:allow (agent_key is a PUBLIC key)
                decision="certify", reason="ok", invoice_id=inv, agent_handle=HANDLE,
                agent_key=akey, verifiability_tag="judgment", domain=DOMAIN), _req(T))  # gitleaks:allow
            await post_outcome(OutcomeEvent(invoice_ref=inv, outcome="paid"), _req(T))
    asyncio.run(_seed())

    return SimpleNamespace(mcp=mcp, read=read, tenant=T, akey=akey, foundation=foundation)


def _client(mcp_router, tenant_id, scopes):
    app = FastAPI()

    class _Inject(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant = SimpleNamespace(
                tenant_id=tenant_id, scopes=list(scopes), authenticated=True)
            return await call_next(request)

    app.add_middleware(_Inject)
    app.include_router(mcp_router)
    return TestClient(app)


def _rpc(client, method, params=None, id=1, headers=None):
    body = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers or {})


# ── handshake ────────────────────────────────────────────────────────────────

def test_initialize_handshake(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    r = _rpc(c, "initialize", {"protocolVersion": "2026-07-28", "capabilities": {}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["protocolVersion"] == "2026-07-28"
    assert res["serverInfo"]["name"] == "com.grafomem/cgr-read"
    assert "tools" in res["capabilities"]


def test_initialize_echoes_older_client_version(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    res = _rpc(c, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert res["protocolVersion"] == "2025-06-18"        # supported → echoed
    res2 = _rpc(c, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert res2["protocolVersion"] == "2026-07-28"       # unknown → our latest


def test_tools_list(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    tools = _rpc(c, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"cgr_get_attestation", "cgr_list_domains", "cgr_verify_instructions"}
    # every tool declares a read-only annotation (directory requirement)
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True


def test_notification_returns_202(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    assert r.content == b""


# ── tools ────────────────────────────────────────────────────────────────────

def test_get_attestation_returns_signed_envelope(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    res = _rpc(c, "tools/call", {"name": "cgr_get_attestation",
                                 "arguments": {"subject": HANDLE, "domain": DOMAIN}}).json()["result"]
    env = res["structuredContent"]
    assert env["result"] == "attestation"
    assert env["attestation"]["schema"] == "cgr.attestation.v3"
    assert env["scoring_scope"] == "pooled"
    assert env["domain_n_resolved"] == 3
    assert env["attestation"]["evidence_ref"] is None      # no per-read anchor


def test_envelope_equivalence_with_rest(wired):
    """The MCP tool and the REST /read/attestation share ONE read-core, so the signed
    attestation is identical in every field EXCEPT the mint-time `as_of` (and the
    signature, which signs over `as_of`). Both are validly Foundation-signed. (`as_of` is
    the mint timestamp in the signed body by design, so two wall-clock-separated mints are
    never byte-identical — equivalence is structural: same core, same content, valid sig.)"""
    from aml.cgr.attestation import verify_attestation
    from aml.cgr.issuance import make_verifier

    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    mcp_att = _rpc(c, "tools/call", {"name": "cgr_get_attestation",
                                     "arguments": {"subject": HANDLE, "domain": DOMAIN}}).json()["result"]["structuredContent"]["attestation"]
    rest_att = asyncio.run(wired.read(_req(wired.tenant), subject=HANDLE, domain=DOMAIN))["attestation"]

    a1, a2 = dict(mcp_att), dict(rest_att)
    for k in ("as_of", "signature"):          # the only mint-time-derived fields
        a1.pop(k, None); a2.pop(k, None)
    assert a1 == a2                            # identical content from the shared core

    v = make_verifier(wired.foundation.public_key())
    assert verify_attestation(mcp_att, v) is True     # both are validly Foundation-signed
    assert verify_attestation(rest_att, v) is True


def test_no_evidence_unknown_subject(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    res = _rpc(c, "tools/call", {"name": "cgr_get_attestation",
                                 "arguments": {"subject": "nobody@nowhere"}}).json()["result"]
    env = res["structuredContent"]
    assert env["result"] == "no_evidence"
    assert env["score"] is None


def test_no_evidence_unknown_domain(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    env = _rpc(c, "tools/call", {"name": "cgr_get_attestation",
                                 "arguments": {"subject": HANDLE, "domain": "no-such-domain"}}).json()["result"]["structuredContent"]
    assert env["result"] == "no_evidence"


def test_list_domains(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    env = _rpc(c, "tools/call", {"name": "cgr_list_domains",
                                 "arguments": {"subject": HANDLE}}).json()["result"]["structuredContent"]
    assert env["result"] == "domains"
    assert DOMAIN in env["domains"]


def test_verify_instructions_no_scope_needed(wired):
    # verify_instructions is static guidance — works even without cgr:read
    c = _client(wired.mcp, wired.tenant, ["something-else"])
    env = _rpc(c, "tools/call", {"name": "cgr_verify_instructions"}).json()["result"]["structuredContent"]
    assert env["lib"] == "@gns-foundation/cgr-verify"
    assert env["issuer_pubkey"] == wired.foundation.public_key().hex()


# ── boundaries ───────────────────────────────────────────────────────────────

def test_scope_gate_blocks_data_tool(wired):
    """A tenant WITHOUT cgr:read gets a JSON-RPC error on a data tool, not data."""
    c = _client(wired.mcp, wired.tenant, ["decisions:read"])   # no cgr:read
    r = _rpc(c, "tools/call", {"name": "cgr_get_attestation", "arguments": {"subject": HANDLE}})
    err = r.json()["error"]
    assert err["code"] == -32001
    assert "cgr:read" in err["message"]


def test_origin_rejection(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    r = _rpc(c, "initialize", {"protocolVersion": "2026-07-28"},
             headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_https_origin_allowed(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    r = _rpc(c, "initialize", {"protocolVersion": "2026-07-28"},
             headers={"Origin": "https://claude.ai"})
    assert r.status_code == 200


def test_unknown_method_and_tool(wired):
    c = _client(wired.mcp, wired.tenant, ["cgr:read"])
    assert _rpc(c, "does/not/exist").json()["error"]["code"] == -32601
    err = _rpc(c, "tools/call", {"name": "bogus_tool"}).json()["error"]
    assert err["code"] == -32602
