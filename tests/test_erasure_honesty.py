"""0016 — erasure certificates are FACTUAL (must-fail tests).

Each test here pins one of the item-1 honesty fixes and FAILS on the pre-0016 code:

  * a certificate is REFUSED when the primary store still holds the fact (was: issued,
    with ``coverage.primary == "present"`` tucked inside a signed "Article 17" artifact);
  * ``coverage_gaps`` is DERIVED from the probe (was: hard-coded ``[]  # Verified offline``);
  * the delete endpoint binds a SERVER-OBSERVED digest of the erased content (was: null);
  * ``/v1/erasure/issue`` no longer honours a caller-supplied ``signing_key`` (was: a
    "platform-signed" certificate under a key of the caller's choosing);
  * regulatory findings are COMPUTED from configuration and data (was: three sections
    hard-coded ``"COMPLIANT"`` and ``erasure_proof_active = ... or True``).

The MCP ``verify_erasure`` fabrication fix is pinned in test_mcp_governance.py
(``sealed_probe is None``; ``ERASED_VERIFIED`` only via a real reverify probe).

Non-vacuity discipline: flip each guard and watch the test break before trusting it.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aml.cloud.erasure_proof import (
    ErasureProofService,
    FactStillPresent,
    VERIFIED_STATUSES,
    hash_content,
)


# ── hermetic helpers (mirror tests/test_erasure_i0b.py) ──────────────────────

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
        return Ed25519PrivateKey.from_private_bytes(self.k).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)


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
    def scrub_fact(self, fact_ref, tenant_id, **k): return 0


class _FakeLedger:
    def __init__(self, events): self.events = events
    def record_subject_erasure(self, **kw): self.events.append("ledger_write")


class _ProbeBackend:
    """Answers the 0014 coverage probe: exists(ref) -> present / absent."""
    def __init__(self, present): self._present = present
    def capabilities(self):
        from aml.backends.interface import Capability
        return {Capability.POINT_LOOKUP}
    def exists(self, ref): return self._present


def _svc(events):
    s = ErasureProofService(db_url=None, decision_trail=_FakeDT(),
                            signing_identity=_MockId(), erasure_ledger=_FakeLedger(events))
    s._conn = _FakeConn(events)
    return s


# ── 1d: a still-present fact is REFUSED, and leaves no orphan rows ───────────

def test_present_fact_refuses_and_leaves_no_orphan_rows():
    events: list[str] = []
    svc = _svc(events)

    with pytest.raises(FactStillPresent):
        svc.issue_certificate("t1", 42, backend=_ProbeBackend(present=True))

    # Refusal is ordered BEFORE the ledger write and the certificate insert:
    # nothing was ledgered, nothing was persisted.
    assert "ledger_write" not in events
    assert "cert_insert" not in events


def test_absent_fact_still_issues_so_the_gate_is_not_vacuous():
    """Same service, same backend class, flipped observation → certificate issued.
    Proves the refusal above comes from the 'present' observation, not from the
    probe path being broken."""
    events: list[str] = []
    svc = _svc(events)

    cert = svc.issue_certificate("t1", 42, backend=_ProbeBackend(present=False))

    assert cert.coverage["primary"] == "absent"
    assert events == ["ledger_write", "cert_insert"]  # ledger first (0013), then cert


# ── 1d: coverage_gaps is derived from the probe, never asserted ─────────────

def test_coverage_gaps_are_derived_from_the_probe():
    events: list[str] = []
    svc = _svc(events)

    cert = svc.issue_certificate(
        "t1", 7, backend=_ProbeBackend(present=False), declared_subsystems=["kafka"],
    )

    gaps = cert.governance_record["non_claims"]["coverage_gaps"]
    expected = sorted(s for s, st in cert.coverage.items() if st not in VERIFIED_STATUSES)
    assert gaps == expected
    # Stage 1 does not probe embedding/cache and never probes a declared extra, so
    # the gap list is NON-EMPTY — the pre-0016 code returned [] here.
    assert gaps, "coverage_gaps must not be an asserted empty list"
    assert "kafka" in gaps and "primary" not in gaps


# ── 1d / 1c: route behaviour ────────────────────────────────────────────────

def _router_app(service):
    from fastapi import FastAPI, Request
    from aml.cloud.erasure_routes import create_erasure_router

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.tenant = SimpleNamespace(tenant_id="t1", role="admin", scopes=["*"])
        return await call_next(request)

    app.include_router(create_erasure_router(service))
    return app


def test_route_maps_fact_still_present_to_409():
    from fastapi.testclient import TestClient

    class _SvcPresent:
        def issue_certificate(self, **kw):
            raise FactStillPresent("refuse: primary store still holds memory_ref:1")

    r = TestClient(_router_app(_SvcPresent())).post("/v1/erasure/issue", json={"fact_ref": 1})
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["status"] == "not_issued"
    assert body["reason"] == "fact_still_present"
    assert body["fact_ref"] == 1


def test_route_no_longer_accepts_a_caller_signing_key():
    """A caller-supplied signing_key must not reach the service. Pre-0016 the route
    built a SigningIdentity from it and passed signing_identity=<caller key>."""
    from fastapi.testclient import TestClient

    captured: dict = {}

    class _SvcRecord:
        def issue_certificate(self, **kw):
            captured.update(kw)
            return SimpleNamespace(
                certificate_id="c1", tenant_id="t1", fact_ref=1, fact_content_hash=None,
                coverage={"primary": "absent"}, scrubbed_decision_ids=[],
                erasure_requested_at=datetime.now(timezone.utc),
                erasure_completed_at=datetime.now(timezone.utc),
                legal_basis="GDPR Article 17 — Right to Erasure", requested_by="data_subject",
                signature=b"s", public_key=b"p", verified=True,
                verification_note="Ed25519-signed at issuance", governance_record=None,
            )

    r = TestClient(_router_app(_SvcRecord())).post(
        "/v1/erasure/issue",
        json={"fact_ref": 1, "signing_key": "ab" * 32, "memory_deleted": True},
    )
    assert r.status_code == 200, r.text
    # The field is gone from the model, so it can't be forwarded; and no identity
    # derived from it is passed. (memory_deleted is likewise ignored, not a 422:
    # older SDKs still send it.)
    assert "signing_identity" not in captured
    assert "signing_key" not in captured
    assert "memory_deleted" not in captured


# ── 1e: regulatory findings are computed, never constant ────────────────────

class _FakeEP:
    def __init__(self, total, signed, can_sign=True):
        self._stats = {"total": total, "signed_count": signed, "total_scrubbed": 0,
                       "first_erasure": None, "last_erasure": None}
        self._can_sign = can_sign

    def get_stats(self, tenant_id): return dict(self._stats)

    def assert_can_sign(self, signing_identity=None):
        if not self._can_sign:
            raise RuntimeError("no signing key")


class _FakeGov:
    def __init__(self, enabled_types):
        self._types = list(enabled_types)

    def list_policies(self, tenant_id, enabled_only=False):
        return [SimpleNamespace(policy_type=t, enabled=True) for t in self._types]

    def get_stats(self, tenant_id):
        return {"policies_total": len(self._types), "policies_active": len(self._types),
                "evaluations_total": 0, "denials_total": 0, "escalations_total": 0,
                "evaluations_by_type": {}}


def _reports(ep, gov):
    from aml.cloud.regulatory import RegulatoryReportService
    svc = RegulatoryReportService(db_url=None, erasure_proof=ep, governance=gov)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    return svc._gen_gdpr("t1", start, end), svc._gen_dora("t1", start, end)


def test_regulatory_findings_are_not_constant_when_nothing_is_configured():
    gdpr, dora = _reports(ep=None, gov=None)

    a17 = gdpr["sections"]["article_17_erasure"]
    assert a17["finding"] != "COMPLIANT"
    assert a17["finding"] == "PARTIAL"
    assert a17["compliance_evidence"]["erasure_proof_active"] is False  # was: `... or True`

    a25 = gdpr["sections"]["article_25_by_design"]
    assert a25["finding"] == "PARTIAL"
    assert a25["compliance_evidence"]["cryptographic_signing_configured"] is False
    assert "not physical segregation" in a25["compliance_evidence"]["tenant_isolation"]

    a9 = dora["sections"]["article_9_protection"]
    assert a9["finding"] == "PARTIAL"
    ev = a9["compliance_evidence"]
    assert ev["rate_limiting"] is False and ev["pii_detection"] is False \
        and ev["content_filtering"] is False  # were: hard-coded True
    assert "not applied inline to memory operations" in ev["policy_enforcement_scope"]


def test_regulatory_findings_become_compliant_only_when_earned():
    ep = _FakeEP(total=2, signed=2)
    gov = _FakeGov(["rate_limit", "pii_guard", "content_filter"])
    gdpr, dora = _reports(ep, gov)

    assert gdpr["sections"]["article_17_erasure"]["finding"] == "COMPLIANT"
    assert gdpr["sections"]["article_25_by_design"]["finding"] == "COMPLIANT"
    a9 = dora["sections"]["article_9_protection"]
    assert a9["finding"] == "COMPLIANT"
    assert a9["compliance_evidence"]["rate_limiting"] is True

    # Flip ONE input each and the finding must move — the computation is live.
    gdpr_unsigned, _ = _reports(_FakeEP(total=2, signed=1), gov)
    assert gdpr_unsigned["sections"]["article_17_erasure"]["finding"] == "PARTIAL"

    gdpr_empty, _ = _reports(_FakeEP(total=0, signed=0), gov)
    assert gdpr_empty["sections"]["article_17_erasure"]["finding"] == "INSUFFICIENT_DATA"

    _, dora_no_rate = _reports(ep, _FakeGov(["pii_guard", "content_filter"]))
    assert dora_no_rate["sections"]["article_9_protection"]["finding"] == "PARTIAL"
    assert dora_no_rate["sections"]["article_9_protection"]["compliance_evidence"]["rate_limiting"] is False


# ── 1d: the delete endpoint binds a SERVER-OBSERVED digest (end to end) ─────

@pytest.fixture(scope="module")
def _e2e():
    """Real app + real Postgres (cloud mode), like tests/test_integration_seams.py."""
    db_url = os.environ.get("GRAFOMEM_DB_URL")
    if not db_url:
        pytest.skip("GRAFOMEM_DB_URL not set")
    from fastapi.testclient import TestClient
    from aml.server.app import create_app
    from aml.cloud.tenant_manager import TenantManager

    os.environ["AUTH_MODE"] = "cloud"
    os.environ.setdefault("GRAFOMEM_SIGNING_KEY", "b" * 64)
    tm = TenantManager(db_url)
    tm.ensure_schema()
    info = tm.create_tenant(name=f"honesty-{uuid.uuid4().hex[:8]}")
    app = create_app(db_url=db_url)
    with TestClient(app) as client:
        yield {"client": client, "db_url": db_url, "admin_key": info.api_key,
               "tenant_id": info.id}


def test_delete_route_binds_server_observed_content_hash(_e2e):
    import psycopg
    from psycopg.rows import dict_row

    c, key = _e2e["client"], _e2e["admin_key"]
    h = {"Authorization": f"Bearer {key}"}

    r = c.post("/v1/stores", headers=h)
    assert r.status_code == 200, r.text
    store_id = r.json()["store_id"]

    content = f"server-observed digest canary {uuid.uuid4().hex}"
    r = c.post(f"/v1/stores/{store_id}/write", headers=h, json={"content": content, "options": {}})
    assert r.status_code == 200, r.text
    ref = r.json()["ref"]

    r = c.post(f"/v1/stores/{store_id}/delete", headers=h, json={"ref": ref})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    cert_id = r.json().get("erasure_certificate_id")
    assert cert_id, "cloud-mode delete must issue a certificate"

    with psycopg.connect(_e2e["db_url"], row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT fact_content_hash, coverage FROM erasure_certificates WHERE certificate_id = %s",
            (cert_id,),
        ).fetchone()
    assert row is not None
    # Pre-0016 this was NULL: the route never read the record before deleting it.
    assert row["fact_content_hash"] == hash_content(content)
    assert row["coverage"]["primary"] == "absent"

    # And the stored hash is bound into the signature: an independent verify passes.
    r = c.get(f"/v1/erasure/{cert_id}/verify", headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("valid") is True or r.json().get("verified") is True
