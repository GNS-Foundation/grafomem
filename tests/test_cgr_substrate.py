"""CGR Ticket #1 — substrate-capture tests.

Exercises the real DecisionTrailService + ExecutionReceiptService + PostgresGMP
outcomes store against the local test DB (see conftest). We drive the actual
route coroutines with a minimal fake Request (state.tenant carries tenant_id +
scopes), which is all the handlers read — so the tag/join/isolation behaviour is
tested end-to-end without spinning the full auth middleware.

Covers the five ticket tests plus the four additions Camilo asked for:
  (1) cross-tenant isolation on the outcomes store + export,
  (2) revisions resolved by latest valid_from with capability-gated supersede,
  (3) idempotent identical re-posts,
  (4) outcomes join for ALL certifies regardless of verifiability_tag.
"""
from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest

from aml.backends.postgres_gmp import PostgresGMPBackend
from aml.cloud.decision_trail import DecisionTrailService
from aml.cloud.execution_receipts import ExecutionReceiptService
from aml.cloud import demo_routes as dr
from aml.cloud.demo_routes import (
    GovernedDecisionRequest, OutcomeEvent, VerifyBatchRequest,
    create_cgr_router, create_governed_router,
)
from aml.server.stores import StoreManager

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"

pytestmark = pytest.mark.asyncio


class _MockId:
    """Ed25519 signing identity: .sign(msg) -> (sig, pub); .public_key() -> pub."""
    def __init__(self, k: bytes | None = None):
        self.k = k or uuid.uuid4().bytes + uuid.uuid4().bytes  # 32 bytes

    def _priv(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(self.k)

    def sign(self, m):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        priv = self._priv()
        return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def public_key(self):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return self._priv().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _req(tenant_id: str, scopes=("*",)):
    return SimpleNamespace(state=SimpleNamespace(
        tenant=SimpleNamespace(tenant_id=tenant_id, scopes=list(scopes))))


def _tenant():
    return f"cgr-{uuid.uuid4().hex[:8]}"


def _endpoint(router, path, method="POST"):
    for r in router.routes:
        if r.path == path and method in r.methods:
            return r.endpoint
    raise KeyError(f"{method} {path} not on router")


# --- shared services (schema ensured by conftest; rows truncated per test) ----

@pytest.fixture(scope="module")
def services():
    ident = _MockId()
    dt = DecisionTrailService(TEST_DB_URL)
    dt.ensure_schema()
    receipts = ExecutionReceiptService(TEST_DB_URL, signing_identity=ident)
    receipts.ensure_schema()
    store_mgr = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL))
    gov = create_governed_router(dt, receipts, ident, store_mgr)
    cgr = create_cgr_router(dt, store_mgr)
    return SimpleNamespace(
        dt=dt, receipts=receipts, ident=ident, store_mgr=store_mgr,
        governed_decision=_endpoint(gov, "/v1/governed/decisions"),
        verify_batch=_endpoint(gov, "/v1/governed/verify-batch"),
        post_outcome=_endpoint(gov, "/v1/governed/outcomes"),
        post_outcomes_bulk=_endpoint(gov, "/v1/governed/outcomes/bulk"),
        export=_endpoint(cgr, "/v1/cgr/substrate/export", "GET"),
    )


# ============================================================================
# Ticket test 1 — decision persists all 3 irreversible fields + reason_code + schema
# ============================================================================

async def test_decision_persists_cgr_fields(services):
    T = _tenant()
    await services.governed_decision(
        GovernedDecisionRequest(decision="certify", reason="ok", invoice_id="INV-A",
                                context={"amount": 100}), _req(T))
    recs = services.dt.query_decisions(tenant_id=T, limit=10)
    assert len(recs) == 1
    p = recs[0].parameters
    assert p["invoice_ref"] == "INV-A"
    assert p["agent_handle"] == "invoice-certifier@kapwork-receivables"
    assert p["verifiability_tag"] == "judgment"
    assert p["reason_code"] is None          # judgment path carries no rule code
    assert p["agent_tier"] is None
    assert p["cgr_schema"] == dr.CGR_DECISION_SCHEMA


# ============================================================================
# Ticket test 2 — verify-batch tags every decision "rule" + emits reason_codes
# ============================================================================

async def test_verify_batch_tags_rule_and_reason_codes(services):
    T = _tenant()
    invoices = [
        {"invoice_id": "INV-1", "po_amount": 100, "invoice_amount": 100, "approval_status": "approved"},
        {"invoice_id": "INV-2", "po_amount": 100, "invoice_amount": 250, "approval_status": "approved"},
        {"invoice_id": "INV-1", "po_amount": 100, "invoice_amount": 100, "approval_status": "approved"},
    ]
    out = await services.verify_batch(VerifyBatchRequest(invoices=invoices), _req(T))
    codes = [r["reason_code"] for r in out["results"]]
    assert codes == ["clean", "amount_exceeds_po", "duplicate"]
    assert out["summary"] == {"total": 3, "certified": 1, "rejected": 2}

    recs = services.dt.query_decisions(tenant_id=T, limit=10)
    assert len(recs) == 3
    assert all(r.parameters["verifiability_tag"] == "rule" for r in recs)
    assert all(r.parameters["agent_handle"] == "invoice-rules-engine@kapwork-receivables" for r in recs)
    assert {r.parameters["reason_code"] for r in recs} == {"clean", "amount_exceeds_po", "duplicate"}


# ============================================================================
# Ticket test 3 — missing invoice_ref logs the warning
# ============================================================================

async def test_missing_invoice_ref_warns(services, caplog):
    T = _tenant()
    with caplog.at_level(logging.WARNING, logger="grafomem.cloud.demo_routes"):
        await services.governed_decision(
            GovernedDecisionRequest(decision="reject", reason="no id", invoice_id=None), _req(T))
    assert any("no invoice_ref" in r.message for r in caplog.records)


# ============================================================================
# Ticket test 4 — outcome writes + revision supersedes (append-only, latest wins)
# ============================================================================

async def test_outcome_write_and_revision_supersedes(services):
    T = _tenant()
    backend = services.store_mgr.get_or_create_named(dr.CGR_OUTCOMES_STORE).backend

    r1 = await services.post_outcome(
        OutcomeEvent(invoice_ref="INV-9", outcome="late", outcome_date="2026-01-01T00:00:00Z"), _req(T))
    assert r1["superseded_prior"] is False and r1["idempotent"] is False

    r2 = await services.post_outcome(
        OutcomeEvent(invoice_ref="INV-9", outcome="default", outcome_date="2026-02-01T00:00:00Z",
                     amount_recovered=0.0, source="funder_feed"), _req(T))
    assert r2["superseded_prior"] is True

    recs = dr._tenant_outcomes(backend, T)
    for_ref = [m for m in recs if (m.metadata or {}).get("subject") == "INV-9"]
    assert len(for_ref) == 2                          # append-only: prior NOT deleted
    assert dr._latest_for(recs, "INV-9").metadata["object"] == "default"
    # Postgres backend supports SUPERSESSION_CHAIN → prior is flagged, not deleted
    prior = min(for_ref, key=dr._sort_key)
    assert prior.superseded_by is not None


# ============================================================================
# Ticket test 5 / addition 4 — export joins outcomes for ALL decisions, any tag
# ============================================================================

async def test_export_joins_outcomes_regardless_of_tag(services):
    T = _tenant()
    # a rule-tagged batch (INV-1 certify, INV-2 reject) ...
    await services.verify_batch(VerifyBatchRequest(invoices=[
        {"invoice_id": "INV-1", "po_amount": 100, "invoice_amount": 100, "approval_status": "approved"},
        {"invoice_id": "INV-2", "po_amount": 100, "invoice_amount": 250, "approval_status": "approved"},
    ]), _req(T))
    # ... and a judgment-tagged decision on INV-J
    await services.governed_decision(
        GovernedDecisionRequest(decision="certify", reason="human ok", invoice_id="INV-J"), _req(T))

    # outcomes for INV-1 (rule) and INV-J (judgment); INV-2 left unresolved
    await services.post_outcome(OutcomeEvent(invoice_ref="INV-1", outcome="paid"), _req(T))
    await services.post_outcome(OutcomeEvent(invoice_ref="INV-J", outcome="default"), _req(T))

    exp = await services.export(_req(T))
    by_ref = {r["invoice_ref"]: r for r in exp["decisions"]}
    assert exp["count"] == 3
    assert by_ref["INV-1"]["outcome"] == "paid" and by_ref["INV-1"]["verifiability_tag"] == "rule"
    assert by_ref["INV-J"]["outcome"] == "default" and by_ref["INV-J"]["verifiability_tag"] == "judgment"
    assert by_ref["INV-2"]["outcome"] is None        # unresolved → null


# ============================================================================
# Addition 1 — cross-tenant isolation on outcomes store + export
# ============================================================================

async def test_cross_tenant_isolation(services):
    A, B = _tenant(), _tenant()
    # tenant A records a decision + outcome for INV-X
    await services.governed_decision(
        GovernedDecisionRequest(decision="certify", reason="", invoice_id="INV-X"), _req(A))
    await services.post_outcome(OutcomeEvent(invoice_ref="INV-X", outcome="paid"), _req(A))

    # tenant B must NOT see A's outcome via the store helper or the export
    backend = services.store_mgr.get_or_create_named(dr.CGR_OUTCOMES_STORE).backend
    assert dr._tenant_outcomes(backend, B) == []
    exp_b = await services.export(_req(B))
    assert exp_b["count"] == 0

    # and A still sees its own
    exp_a = await services.export(_req(A))
    assert exp_a["count"] == 1 and exp_a["decisions"][0]["outcome"] == "paid"


# ============================================================================
# Addition 2 — export resolves the LATEST outcome by valid_from
# ============================================================================

async def test_export_uses_latest_outcome(services):
    T = _tenant()
    await services.governed_decision(
        GovernedDecisionRequest(decision="certify", reason="", invoice_id="INV-R"), _req(T))
    await services.post_outcome(
        OutcomeEvent(invoice_ref="INV-R", outcome="paid", outcome_date="2026-01-01T00:00:00Z"), _req(T))
    await services.post_outcome(
        OutcomeEvent(invoice_ref="INV-R", outcome="written_off", outcome_date="2026-03-01T00:00:00Z",
                     source="funder_feed"), _req(T))
    exp = await services.export(_req(T))
    row = next(r for r in exp["decisions"] if r["invoice_ref"] == "INV-R")
    assert row["outcome"] == "written_off"
    assert row["outcome_date"].startswith("2026-03-01")


# ============================================================================
# Addition 3 — an identical re-post is idempotent (no new record, no supersede)
# ============================================================================

async def test_idempotent_identical_repost(services):
    T = _tenant()
    backend = services.store_mgr.get_or_create_named(dr.CGR_OUTCOMES_STORE).backend
    ev = OutcomeEvent(invoice_ref="INV-ID", outcome="paid", amount_recovered=500.0, source="kapwork_ledger")

    r1 = await services.post_outcome(ev, _req(T))
    r2 = await services.post_outcome(ev, _req(T))          # byte-identical
    assert r1["idempotent"] is False
    assert r2["idempotent"] is True and r2["superseded_prior"] is False

    for_ref = [m for m in dr._tenant_outcomes(backend, T) if (m.metadata or {}).get("subject") == "INV-ID"]
    assert len(for_ref) == 1                                # no duplicate row written


# ============================================================================
# Bulk convenience path
# ============================================================================

async def test_outcomes_bulk(services):
    T = _tenant()
    backend = services.store_mgr.get_or_create_named(dr.CGR_OUTCOMES_STORE).backend
    out = await services.post_outcomes_bulk([
        OutcomeEvent(invoice_ref="B1", outcome="paid"),
        OutcomeEvent(invoice_ref="B2", outcome="default"),
    ], _req(T))
    assert out["count"] == 2
    refs = {(m.metadata or {}).get("subject") for m in dr._tenant_outcomes(backend, T)}
    assert {"B1", "B2"} <= refs
