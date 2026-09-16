"""I0b — ledger-before-certificate must-fail tests (decision 0013). Hermetic (no DB).

A certificate must NEVER exist without a prior restore-scrub ledger row. These pin the
three gates + the ordering. They FAIL on pre-I0b code (which defaults empty coverage,
persists unsigned certs, and skips the ledger when it's absent) — watch them fail first.
"""
import pytest

from aml.cloud.erasure_proof import (
    ErasureProofService,
    EmptyGovernanceCoverage,
    UnsignedErasure,
    LedgerRequired,
    CertificateNotIssued,
)


class _MockId:
    def __init__(self, k=b"0" * 32):
        self.k = k

    def sign(self, m):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        priv = Ed25519PrivateKey.from_private_bytes(self.k)
        return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return Ed25519PrivateKey.from_private_bytes(self.k).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


class _Result:
    def fetchall(self): return []
    def fetchone(self): return None


class _FakeConn:
    closed = False

    def __init__(self, events):
        self.events = events

    def execute(self, sql, params=None):
        if "INSERT INTO" in sql and "erasure_certificates" in sql:
            self.events.append("cert_insert")
        return _Result()


class _FakeDT:
    def __init__(self): self.scrubbed = []
    def scrub_fact(self, fact_ref, tenant_id, **k): self.scrubbed.append(fact_ref); return 0


class _FakeLedger:
    def __init__(self, events, fail=False): self.events, self.fail, self.rows = events, fail, []
    def record_subject_erasure(self, **kw):
        if self.fail:
            raise RuntimeError("ledger pool down")
        self.events.append("ledger_write")
        self.rows.append(kw)


def _svc(events, ledger, dt=None):
    s = ErasureProofService(db_url=None, decision_trail=dt if dt is not None else _FakeDT(),
                            signing_identity=_MockId(), erasure_ledger=ledger)
    s._conn = _FakeConn(events)  # inject fake conn; no real DB
    return s


def _cert_inserted(events): return "cert_insert" in events


# ── gate 1: empty governance/coverage → refuse (no cert) ─────────────────────
def test_empty_coverage_refuses():
    ev = []
    svc = _svc(ev, _FakeLedger(ev))
    with pytest.raises(EmptyGovernanceCoverage):
        svc.issue_certificate("t1", 123, coverage={})
    assert not _cert_inserted(ev), "no certificate may be persisted for empty coverage"


# ── gate 2: unsigned → refuse (no cert) ──────────────────────────────────────
def test_unsigned_refuses(monkeypatch):
    monkeypatch.setattr("aml.provenance.sign_provenance", lambda key, digest: (None, None))
    ev = []
    svc = _svc(ev, _FakeLedger(ev))
    with pytest.raises(UnsignedErasure):
        svc.issue_certificate("t1", 123, coverage={"primary": "absent"})
    assert not _cert_inserted(ev), "no certificate may be persisted when signing fails"


# ── gate 3: unconfigured ledger (default) → refuse, and DO NOT erase ──────────
def test_unconfigured_ledger_refuses_and_does_not_erase(monkeypatch):
    monkeypatch.delenv("ERASURE_LEDGER_OPTIONAL", raising=False)
    ev = []
    dt = _FakeDT()
    svc = _svc(ev, None, dt=dt)  # ledger unconfigured
    with pytest.raises(LedgerRequired):
        svc.issue_certificate("t1", 123, coverage={"primary": "absent"})
    assert not _cert_inserted(ev), "no certificate without a ledger"
    assert dt.scrubbed == [], "default mode must NOT erase when the ledger is unavailable"


# ── ERASURE_LEDGER_OPTIONAL: erase, but NO certificate ───────────────────────
def test_optional_ledger_erases_without_certificate(monkeypatch):
    monkeypatch.setenv("ERASURE_LEDGER_OPTIONAL", "1")
    ev = []
    dt = _FakeDT()
    svc = _svc(ev, None, dt=dt)
    with pytest.raises(CertificateNotIssued):
        svc.issue_certificate("t1", 123, coverage={"primary": "absent"})
    assert dt.scrubbed == [123], "optional mode still erases the fact"
    assert not _cert_inserted(ev), "optional mode issues NO certificate"


# ── ordering: the ledger row is written BEFORE the certificate is persisted ───
def test_ledger_written_before_certificate(monkeypatch):
    monkeypatch.delenv("ERASURE_LEDGER_OPTIONAL", raising=False)
    ev = []
    ledger = _FakeLedger(ev)
    svc = _svc(ev, ledger)
    cert = svc.issue_certificate("t1", 123, coverage={"primary": "absent"})
    assert cert is not None and ledger.rows, "a ledger row must be written"
    assert ev.index("ledger_write") < ev.index("cert_insert"), "ledger must be committed before the cert"


# ── ledger write failure → refuse (no cert); orphan-ledger is the safe residue ─
def test_ledger_write_failure_refuses_certificate(monkeypatch):
    monkeypatch.delenv("ERASURE_LEDGER_OPTIONAL", raising=False)
    ev = []
    svc = _svc(ev, _FakeLedger(ev, fail=True))
    with pytest.raises(Exception):
        svc.issue_certificate("t1", 123, coverage={"primary": "absent"})
    assert not _cert_inserted(ev), "a failing ledger write must abort before the cert is persisted"


# ── Site 2 (tenant destruction) — ledger-before-destroy, fail-closed ─────────
import asyncio
from types import SimpleNamespace
from fastapi import HTTPException
from aml.cloud.admin_routes import destroy_tenant_key, DestroyKeyRequest

_CONFIRM = "I understand this is irreversible"


class _TKM:
    def __init__(self): self.destroyed = []
    def destroy_tenant_key(self, tid): self.destroyed.append(tid); return "ok"


def _destroy(state):
    req = SimpleNamespace(app=SimpleNamespace(state=state))
    return destroy_tenant_key("t1", DestroyKeyRequest(confirmation=_CONFIRM), req, user={"tenant_id": "t1"})


def test_site2_ledger_absent_503_before_destruction():
    tkm = _TKM()
    state = SimpleNamespace(tenant_key_manager=tkm, erasure_ledger=None, signing_identity=_MockId())
    with pytest.raises(HTTPException) as e:
        asyncio.run(_destroy(state))
    assert e.value.status_code == 503
    assert tkm.destroyed == [], "DEK must NOT be destroyed when the ledger is absent"


def test_site2_ledger_write_exception_keeps_dek():
    class _FailLedger:
        def record_tenant_destruction(self, *a, **k): raise RuntimeError("ledger pool down")
    tkm = _TKM()
    state = SimpleNamespace(tenant_key_manager=tkm, erasure_ledger=_FailLedger(), signing_identity=_MockId())
    with pytest.raises(Exception):
        asyncio.run(_destroy(state))
    assert tkm.destroyed == [], "a failing ledger write must abort before the DEK is destroyed"


# ── route mapping: CertificateNotIssued → 200 "erased, no certificate", not 500 ─
def test_route_maps_certificate_not_issued_to_200():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from aml.cloud.erasure_routes import create_erasure_router

    class _SvcOptional:
        def issue_certificate(self, **kw):
            raise CertificateNotIssued("erased, no certificate issued: ledger not configured")

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.tenant = SimpleNamespace(tenant_id="t1", role="admin", scopes=["*"])
        return await call_next(request)

    app.include_router(create_erasure_router(_SvcOptional()))
    r = TestClient(app).post("/v1/erasure/issue", json={"fact_ref": 1})
    assert r.status_code == 200, r.text  # NOT 500
    assert r.json()["detail"] == "erased, no certificate issued: ledger not configured"
    assert r.json()["certificate_id"] is None
