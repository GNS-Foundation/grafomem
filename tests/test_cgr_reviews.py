"""CGR Ticket #3 — review capture tests.

Round-trip, many-per-invoice (not collapsed), per-(invoice,reviewer) revision +
idempotency, rating validation, tenant isolation, and the integration test that
proves the captured reviewer signal actually moves a score vs reviews=[]. Plus the
two score-time-attribution cases: an agent_handle=None review still attributed via
the join, and an orphan review (no matching decision) that doesn't crash scoring.

Same fake-Request + local-Postgres harness as test_cgr_substrate / test_cgr_scoring.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aml.cgr import compute_scores, load_reviews, reviewer_weights

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"

AGENT = "invoice-certifier@kapwork-receivables"


class _MockId:
    def __init__(self, k: bytes | None = None):
        self.k = k or uuid.uuid4().bytes + uuid.uuid4().bytes

    def _priv(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(self.k)

    def sign(self, m):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        p = self._priv()
        return p.sign(m), p.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def public_key(self):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return self._priv().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _req(tenant_id, scopes=("*",)):
    return SimpleNamespace(state=SimpleNamespace(
        tenant=SimpleNamespace(tenant_id=tenant_id, scopes=list(scopes))))


def _tenant():
    return f"cgrv-{uuid.uuid4().hex[:8]}"


def _ep(router, path, method="POST"):
    for r in router.routes:
        if r.path == path and method in r.methods:
            return r.endpoint
    raise KeyError(f"{method} {path} not found")


@pytest.fixture(scope="module")
def db():
    from aml.cloud.decision_trail import DecisionTrailService
    from aml.cloud.execution_receipts import ExecutionReceiptService
    from aml.cloud.demo_routes import (
        GovernedDecisionRequest, OutcomeEvent, ReviewRecord,
        create_cgr_router, create_governed_router,
    )
    from aml.server.stores import StoreManager
    from aml.backends.postgres_gmp import PostgresGMPBackend

    ident = _MockId()
    dt = DecisionTrailService(TEST_DB_URL); dt.ensure_schema()
    receipts = ExecutionReceiptService(TEST_DB_URL, signing_identity=ident); receipts.ensure_schema()
    sm = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL))
    gov = create_governed_router(dt, receipts, ident, sm)
    exp = create_cgr_router(dt, sm)
    return SimpleNamespace(
        dt=dt, sm=sm,
        GDR=GovernedDecisionRequest, OE=OutcomeEvent, RR=ReviewRecord,
        governed_decision=_ep(gov, "/v1/governed/decisions"),
        post_outcome=_ep(gov, "/v1/governed/outcomes"),
        post_review=_ep(gov, "/v1/governed/reviews"),
        post_reviews_bulk=_ep(gov, "/v1/governed/reviews/bulk"),
        export=_ep(exp, "/v1/cgr/substrate/export", "GET"),
    )


# -- small async helpers -------------------------------------------------------

async def _certify(db, T, inv, agent=AGENT):
    await db.governed_decision(db.GDR(decision="certify", reason="", invoice_id=inv, agent_handle=agent), _req(T))


async def _outcome(db, T, inv, oc):
    await db.post_outcome(db.OE(invoice_ref=inv, outcome=oc), _req(T))


async def _review(db, T, inv, reviewer, rating, agent=AGENT, **kw):
    return await db.post_review(
        db.RR(invoice_ref=inv, reviewer_handle=reviewer, rating=rating, agent_handle=agent, **kw), _req(T))


def _score(db, T, handle, *, reviews=None):
    res = compute_scores(db.dt, db.sm, T, reviews=reviews)
    return next((r for r in res if r.agent_handle == handle), None)


# ============================================================================
# Write path + load_reviews
# ============================================================================

@pytest.mark.asyncio
async def test_review_roundtrip(db):
    T = _tenant()
    await _review(db, T, "INV1", "rev-a", 0.8)
    evs = load_reviews(db.sm, T)
    assert len(evs) == 1
    e = evs[0]
    assert (e.invoice_ref, e.reviewer, e.rating, e.agent_handle) == ("INV1", "rev-a", 0.8, AGENT)


@pytest.mark.asyncio
async def test_many_reviewers_per_invoice_not_collapsed(db):
    T = _tenant()
    await _review(db, T, "INV1", "rev-a", 0.9)
    await _review(db, T, "INV1", "rev-b", 0.2)
    evs = load_reviews(db.sm, T)
    assert len(evs) == 2                                   # two distinct (invoice, reviewer) pairs
    assert {e.reviewer for e in evs} == {"rev-a", "rev-b"}
    assert {(e.invoice_ref) for e in evs} == {"INV1"}


@pytest.mark.asyncio
async def test_review_revision_supersedes_own_prior_and_idempotent(db):
    T = _tenant()
    await _review(db, T, "INV1", "rev-a", 0.3)
    await _review(db, T, "INV1", "rev-b", 0.5)             # different reviewer — untouched
    rev = await _review(db, T, "INV1", "rev-a", 0.9)       # rev-a re-rates
    assert rev["superseded_prior"] is True
    idem = await _review(db, T, "INV1", "rev-a", 0.9)      # identical re-post
    assert idem["idempotent"] is True and idem["superseded_prior"] is False

    evs = {e.reviewer: e for e in load_reviews(db.sm, T)}
    assert evs["rev-a"].rating == 0.9                      # latest per (invoice, reviewer) wins
    assert evs["rev-b"].rating == 0.5                      # the other reviewer untouched


@pytest.mark.asyncio
async def test_rating_out_of_range_rejected(db):
    T = _tenant()
    for bad in (1.5, -0.1):
        with pytest.raises(HTTPException) as ei:
            await _review(db, T, "INV1", "rev-a", bad)
        assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_reviews_tenant_isolation(db):
    A, B = _tenant(), _tenant()
    await _review(db, A, "INV1", "rev-a", 0.7)
    assert len(load_reviews(db.sm, A)) == 1
    assert load_reviews(db.sm, B) == []                    # B sees none of A's reviews


@pytest.mark.asyncio
async def test_reviews_bulk(db):
    T = _tenant()
    out = await db.post_reviews_bulk([
        db.RR(invoice_ref="INV1", reviewer_handle="rev-a", rating=0.6, agent_handle=AGENT),
        db.RR(invoice_ref="INV2", reviewer_handle="rev-a", rating=0.4, agent_handle=AGENT),
    ], _req(T))
    assert out["count"] == 2
    assert {e.invoice_ref for e in load_reviews(db.sm, T)} == {"INV1", "INV2"}


# ============================================================================
# Export — additive reviews[]
# ============================================================================

@pytest.mark.asyncio
async def test_export_reviews_additive(db):
    T = _tenant()
    await _certify(db, T, "INV1")
    await _outcome(db, T, "INV1", "paid")
    await _review(db, T, "INV1", "rev-a", 0.8)
    exp = await db.export(_req(T))
    assert set(exp.keys()) == {"decisions", "count", "reviews"}
    assert len(exp["reviews"]) == 1
    r = exp["reviews"][0]
    assert set(r.keys()) == {"invoice_ref", "reviewer_handle", "agent_handle", "rating", "review_date"}
    assert (r["invoice_ref"], r["reviewer_handle"], r["rating"]) == ("INV1", "rev-a", 0.8)


# ============================================================================
# Integration — the reviewer signal moves a score (the point of the ticket)
# ============================================================================

@pytest.mark.asyncio
async def test_reviewer_signal_moves_score_and_calibrates(db):
    T = _tenant()
    resolved = {"R1": "paid", "R2": "paid", "R3": "paid", "R4": "default", "R5": "default", "R6": "default"}
    for inv, oc in resolved.items():
        await _certify(db, T, inv)
        await _outcome(db, T, inv, oc)
        good = 1.0 if oc == "paid" else 0.0
        await _review(db, T, inv, "good", good)            # perfectly calibrated
        await _review(db, T, inv, "bad", 1.0 - good)       # anti-calibrated
    # two UNRESOLVED certifies, rated high by the good reviewer
    for inv in ("U1", "U2"):
        await _certify(db, T, inv)
        await _review(db, T, inv, "good", 1.0)

    # calibration on real captured reviews
    revs = load_reviews(db.sm, T)
    obs = [(e.reviewer, e.rating, 1.0 if resolved.get(e.invoice_ref) == "paid" else 0.0)
           for e in revs if e.invoice_ref in resolved]
    w = reviewer_weights(obs)
    assert w["good"] > 0.9 and w["bad"] < 0.1

    # the early signal on the unresolved certifies moves the score vs reviews=[]
    with_reviews = _score(db, T, AGENT)                    # reviews=None ⇒ auto-load
    without = _score(db, T, AGENT, reviews=())             # forced empty baseline
    assert with_reviews.n_resolved == 6 and with_reviews.n_pending == 2
    assert with_reviews.cgr_score > without.cgr_score


# ============================================================================
# Score-time attribution (the §-refinement)
# ============================================================================

@pytest.mark.asyncio
async def test_review_with_null_agent_handle_still_attributed_via_join(db):
    T = _tenant()
    # give the reviewer a calibrated track record so its weight > floor
    for inv, oc in {"R1": "paid", "R2": "paid", "R3": "default",
                    "R4": "default", "R5": "paid"}.items():
        await _certify(db, T, inv)
        await _outcome(db, T, inv, oc)
        await _review(db, T, inv, "good", 1.0 if oc == "paid" else 0.0)
    # unresolved certify by AGENT, reviewed with agent_handle OMITTED (None)
    await _certify(db, T, "U1")
    await _review(db, T, "U1", "good", 1.0, agent=None)    # client handle absent

    with_reviews = _score(db, T, AGENT)
    without = _score(db, T, AGENT, reviews=())
    # attribution resolved from the decision join (agent_by_ref["U1"] == AGENT),
    # so the null-handle review still moves AGENT's score
    assert with_reviews.cgr_score > without.cgr_score


@pytest.mark.asyncio
async def test_orphan_review_no_matching_decision_does_not_crash(db, caplog):
    import logging
    T = _tenant()
    await _certify(db, T, "R1")
    await _outcome(db, T, "R1", "paid")
    await _review(db, T, "R1", "good", 1.0)
    # a review for an invoice that has NO decision (orphan) + no client handle
    await _review(db, T, "GHOST", "good", 1.0, agent=None)

    with caplog.at_level(logging.WARNING, logger="grafomem.cgr.engine"):
        results = compute_scores(db.dt, db.sm, T)          # must not raise
    handles = {r.agent_handle for r in results}
    assert AGENT in handles                                # real agent still scored
    assert None not in handles and "GHOST" not in handles  # no phantom agent from the orphan
    assert any("capture gap" in r.message for r in caplog.records)  # capture-gap warning emitted
