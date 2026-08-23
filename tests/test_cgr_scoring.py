"""CGR Ticket #2 — scoring-engine tests.

Pure-math tests (no DB) cover beta_prior, reviewer_weights, score_agent, the
whole-band confidence-gated to_tiergate, the dimension axis, cold-start, and the
synthetic-fixture correlation (field version of the reference's −0.99). DB tests
(local Postgres, same fake-Request harness as test_cgr_substrate) cover the
scores route, the byte-identical export after the load_substrate refactor, and
the cgr:read scope gate. A grep test proves import isolation.
"""
from __future__ import annotations

import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aml.cgr import (
    CGRResult, DIMENSION_RECEIVABLES, MIN_RESOLVED_PROVEN,
    beta_prior, compute_scores_from_rows, reviewer_weights, score_agent, to_tiergate,
)
from aml.cgr.attestation import build_attestation, verify_attestation
from aml.cgr.issuance import FoundationIdentity, issuer_key_id, make_signer, make_verifier
from aml.cgr.scoring import CEILING_EPS, N_LIFT
from aml.cgr.substrate import DecisionRow
from aml.cgr.validate import synthetic_substrate, validate_report

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def _row(inv, handle="A@k", decision="certify", tag="judgment", outcome=None, tier=None, key=None):
    return DecisionRow(decision_id=f"dec-{inv}", invoice_ref=inv, agent_handle=handle,
                       agent_tier=tier, decision=decision, reason_code="clean",
                       verifiability_tag=tag, created_at=None, outcome=outcome, outcome_date=None,
                       agent_key=key)


# ============================================================================
# Pure math
# ============================================================================

def test_beta_prior_tier_and_none():
    assert beta_prior(0.75, k=4.0) == (1 + 4 * 0.75, 1 + 4 * 0.25)
    assert beta_prior(None) == (1.0, 1.0)          # neutral prior when tier missing


def test_reviewer_weights_calibration():
    calibrated = [("good", 1.0, 1.0)] * 6          # zero Brier ⇒ w≈1
    miscalib = [("bad", 0.0, 1.0)] * 6             # worst Brier ⇒ w=0
    few = [("few", 0.5, 1.0)] * 3                  # < MIN_REVIEWS ⇒ floor 0.05
    w = reviewer_weights(calibrated + miscalib + few)
    assert w["good"] == pytest.approx(1.0)
    assert w["bad"] == pytest.approx(0.0)
    assert w["few"] == 0.05
    assert reviewer_weights([]) == {}              # tolerate no review data


def test_score_agent_resolved_updates_and_rule_excluded():
    rows = [_row("X", outcome="paid"), _row("Y", outcome="default"),
            _row("R", decision="reject", tag="rule", outcome="default")]  # rule-reject: excluded
    r = score_agent("A@k", rows, {"X": "paid", "Y": "default", "R": "default"}, [], {}, None)
    assert r.n_resolved == 2 and r.n_pending == 0   # only the two judgment-certifies counted
    assert r.cgr_score == pytest.approx((1 + 1) / (1 + 1 + 1 + 1))  # α=2,β=2 ⇒ 0.5


def test_ceiling_cold_start_clamps_even_against_review_farm():
    # n_resolved = 0 with tier present: ceiling pinned at tier + CEILING_EPS.
    # A wall of rave (soft) reviews must NOT inflate the score past it.
    rows = [_row(f"INV{i}", tier=0.3) for i in range(10)]          # certifies, NO outcomes
    reviews = [(f"INV{i}", "rev", 1.0) for i in range(10)]         # all 1.0, unresolved
    r = score_agent("A@k", rows, {}, reviews, {"rev": 1.0}, 0.3)
    assert r.n_resolved == 0
    assert r.cgr_score == pytest.approx(0.3 + CEILING_EPS)         # 0.32 — cold-start clamp holds


def test_ceiling_lifts_with_evidence_and_skipped_when_none():
    # ≥ N_LIFT resolved outcomes ⇒ ceiling lifts to 1.0, calibration shows through
    rows = [_row(f"INV{i}", outcome="paid", tier=0.3) for i in range(N_LIFT)]
    out = {r.invoice_ref: "paid" for r in rows}
    lifted = score_agent("A@k", rows, out, [], {}, 0.3)
    assert lifted.n_resolved == N_LIFT
    assert lifted.cgr_score > 0.8                                  # NOT capped at 0.32 anymore
    # tier=None ⇒ ceiling skipped entirely
    uncapped = score_agent("A@k", rows, out, [], {}, None)
    assert uncapped.cgr_score > 0.9


def test_dimension_axis_present():
    r = CGRResult("A@k", 0.5, 2.0, 0, 0, None, "2026-01-01T00:00:00Z")
    assert r.dimension == DIMENSION_RECEIVABLES == "receivables"
    assert to_tiergate(r)["dimension"] == "receivables"


# ============================================================================
# Identity-key binding (Ticket #5) — aggregate + emit by GEIANT pubkey
# ============================================================================

AK = "ab" * 32   # a stand-in agent GEIANT identity pubkey (64-hex)


def test_subject_key_captured_and_rides_inside_signed_body():
    rows = [_row("X", handle="finance@zurich", outcome="paid", key=AK),
            _row("Y", handle="finance@zurich", outcome="default", key=AK)]
    res = compute_scores_from_rows(rows)
    assert len(res) == 1 and res[0].subject_key == AK
    tg = to_tiergate(res[0])
    assert tg["subject_key"] == AK and tg["agent_handle"] == "finance@zurich"

    fid = FoundationIdentity(bytes.fromhex("11" * 32))
    att = build_attestation(tg, signer=make_signer(fid), issuer_key_id=issuer_key_id(fid))
    assert att["subject_key"] == AK                                     # inside the signed body
    verify = make_verifier(fid.public_key())
    assert verify_attestation(att, verify) is True
    assert verify_attestation({**att, "subject_key": "cd" * 32}, verify) is False  # tamper breaks sig


def test_aggregate_by_key_not_handle():
    # same key, DIFFERENT handles → ONE agent (the key is the identity)
    same_key = [_row("X", handle="finance@zurich", outcome="paid", key=AK),
                _row("Y", handle="finance@osaka", outcome="default", key=AK)]
    r1 = compute_scores_from_rows(same_key)
    assert len(r1) == 1 and r1[0].subject_key == AK

    # same handle, DIFFERENT keys → TWO agents (the handle is only a label)
    diff_keys = [_row("X", handle="finance@zurich", outcome="paid", key="ab" * 32),
                 _row("Y", handle="finance@zurich", outcome="default", key="cd" * 32)]
    r2 = compute_scores_from_rows(diff_keys)
    assert len(r2) == 2
    assert {x.subject_key for x in r2} == {"ab" * 32, "cd" * 32}


def test_legacy_rows_without_key_aggregate_by_handle():
    rows = [_row("X", handle="a@k", outcome="paid"),        # key=None (legacy)
            _row("Y", handle="a@k", outcome="default")]
    res = compute_scores_from_rows(rows)
    assert len(res) == 1 and res[0].agent_handle == "a@k"
    assert res[0].subject_key is None                       # honest null → GEIANT reads as unproven
    assert to_tiergate(res[0])["subject_key"] is None


# ============================================================================
# to_tiergate — whole-band confidence gate (Requirement 1)
# ============================================================================

def _res(score, n_resolved):
    return CGRResult("A@k", score, float(n_resolved) + 2, n_resolved, 0, None, "t")


def test_tiergate_band_is_confidence_gated_end_to_end():
    # floor evidence + high score ⇒ bronze, NEVER gold/silver
    assert to_tiergate(_res(0.95, MIN_RESOLVED_PROVEN))["tier"] == "bronze"
    # score alone can't buy silver/gold without the evidence floor
    assert to_tiergate(_res(0.90, 9))["tier"] == "bronze"     # < 10 resolved
    assert to_tiergate(_res(0.70, 12))["tier"] == "silver"    # ≥10 & ≥0.65
    assert to_tiergate(_res(0.85, 25))["tier"] == "gold"      # ≥20 & ≥0.80
    # below the proven floor ⇒ unproven regardless of score
    assert to_tiergate(_res(0.99, 0))["tier"] == "unproven"


def test_cold_start_is_unproven_not_confident():
    rows = [_row("X")]                               # certify, no outcome yet
    r = compute_scores_from_rows(rows)[0]
    assert r.n_resolved == 0 and r.n_pending == 1
    assert 0.0 < r.cgr_score < 1.0                   # neutral, never a confident 0/1
    assert to_tiergate(r)["tier"] == "unproven"


# ============================================================================
# Headline — synthetic fixture reproduces corr < −0.7 and beats naive
# ============================================================================

def test_synthetic_corr_meets_threshold_and_beats_naive():
    d = synthetic_substrate(with_tier=False)         # tier=None = the live path
    rep = validate_report(d.rows, d.reviews, truth_by_ref=d.truth_by_ref)
    assert rep["corr_cgr_default"] < -0.7, rep["corr_cgr_default"]
    assert rep["beats_naive"]
    assert abs(rep["corr_cgr_default"]) > abs(rep["corr_naive_default"])


def test_synthetic_tier_wired_meets_threshold_after_ceiling_lift():
    # tier-wired path: each agent has ≫ N_LIFT resolved outcomes, so the
    # evidence-gated ceiling lifts and calibration dominates — the old tier+0.02
    # hard-clamp inversion is gone.
    d = synthetic_substrate(with_tier=True)
    rep = validate_report(d.rows, d.reviews, truth_by_ref=d.truth_by_ref)
    assert rep["corr_cgr_default"] < -0.7, rep["corr_cgr_default"]
    assert rep["beats_naive"]


def test_early_signal_before_full_resolution():
    # only 25% of outcomes back yet — CGR should already predict default
    d = synthetic_substrate(with_tier=False, resolved_fraction=0.25)
    rep = validate_report(d.rows, d.reviews, truth_by_ref=d.truth_by_ref)
    # weaker than full resolution but still strongly negative — usable early signal
    assert rep["corr_cgr_default"] < -0.4, rep["corr_cgr_default"]


# ============================================================================
# Import isolation (the ticket's grep, as a test)
# ============================================================================

def test_cgr_package_import_isolation():
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "aml" / "cgr"
    forbidden = re.compile(r"(portal|stripe|billing|landing|admin|sso|static|"
                           r"tenant_manager|execution_receipts|verification|pdf|"
                           r"webhook|metering|replay|erasure|siem)")
    offenders = []
    for f in sorted(root.glob("*.py")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            s = line.strip()
            if (s.startswith("import ") or s.startswith("from ")) and forbidden.search(s):
                offenders.append(f"{f.name}:{i}: {s}")
    assert not offenders, f"forbidden imports in src/aml/cgr: {offenders}"


# ============================================================================
# DB-backed: scores route, export unchanged, scope gate
# ============================================================================

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
    return f"cgrs-{uuid.uuid4().hex[:8]}"


def _ep(router, needle, method="GET"):
    for r in router.routes:
        if needle in r.path and method in r.methods:
            return r.endpoint
    raise KeyError(f"{method} …{needle} not found")


_EXPORT_KEYS = ["decision_id", "invoice_ref", "agent_handle", "agent_tier", "decision",
                "reason_code", "verifiability_tag", "created_at", "outcome", "outcome_date",
                "agent_key",    # 11th key appended (Ticket #5 identity binding)
                "cgr_domain"]   # 12th key appended (Track C capability domain)


@pytest.fixture(scope="module")
def db():
    from aml.cloud.decision_trail import DecisionTrailService
    from aml.cloud.execution_receipts import ExecutionReceiptService
    from aml.cloud.demo_routes import (
        GovernedDecisionRequest, OutcomeEvent, create_cgr_router, create_governed_router,
    )
    from aml.cgr.routes import create_cgr_scoring_router
    from aml.server.stores import StoreManager
    from aml.backends.postgres_gmp import PostgresGMPBackend

    ident = _MockId()
    dt = DecisionTrailService(TEST_DB_URL); dt.ensure_schema()
    receipts = ExecutionReceiptService(TEST_DB_URL, signing_identity=ident); receipts.ensure_schema()
    sm = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL))
    gov = create_governed_router(dt, receipts, ident, sm)
    exp = create_cgr_router(dt, sm)
    sco = create_cgr_scoring_router(dt, sm)
    return SimpleNamespace(
        GovernedDecisionRequest=GovernedDecisionRequest, OutcomeEvent=OutcomeEvent,
        governed_decision=_ep(gov, "/v1/governed/decisions", "POST"),
        post_outcome=_ep(gov, "/v1/governed/outcomes", "POST"),
        export=_ep(exp, "/v1/cgr/substrate/export"),
        scores=_ep(sco, "/v1/cgr/scores", "GET"),
    )


@pytest.mark.asyncio
async def test_scores_route_groups_and_resolves(db):
    T = _tenant()
    A = "invoice-certifier@kapwork-receivables"
    GDR = db.GovernedDecisionRequest
    for inv, oc in [("X", "paid"), ("Y", "default"), ("Z", None)]:
        await db.governed_decision(GDR(decision="certify", reason="", invoice_id=inv, agent_handle=A), _req(T))
        if oc:
            await db.post_outcome(db.OutcomeEvent(invoice_ref=inv, outcome=oc), _req(T))
    resp = await db.scores(_req(T))
    assert resp["dimension"] == "receivables"
    s = next(x for x in resp["scores"] if x["agent_handle"] == A)
    assert s["n_resolved"] == 2 and s["n_pending"] == 1
    assert 0.0 < s["cgr_score"] < 1.0
    assert s["dimension"] == "receivables"


@pytest.mark.asyncio
async def test_scores_route_surfaces_posterior_and_cap_provenance(db):
    # #8a: the live /v1/cgr/scores payload carries the Beta posterior + cap provenance.
    T = _tenant()
    A = "invoice-certifier@kapwork-receivables"
    GDR = db.GovernedDecisionRequest
    for inv, oc in [("X", "paid"), ("Y", "default")]:
        await db.governed_decision(GDR(decision="certify", reason="", invoice_id=inv, agent_handle=A), _req(T))
        await db.post_outcome(db.OutcomeEvent(invoice_ref=inv, outcome=oc), _req(T))
    resp = await db.scores(_req(T))
    s = next(x for x in resp["scores"] if x["agent_handle"] == A)
    a, b = s["post_alpha"], s["post_beta"]
    assert a is not None and b is not None
    assert abs(a / (a + b) - s["cgr_score"]) < 1e-9        # posterior mean == score (no tier ⇒ unbound)
    assert abs((a + b) - s["confidence"]) < 1e-9           # α+β == confidence
    assert s["cap_source"] == "tier_proxy"                 # no J-Space profile in the live path
    assert s["cap_confidence"] is None


@pytest.mark.asyncio
async def test_export_response_shape_unchanged(db):
    T = _tenant()
    await db.governed_decision(
        db.GovernedDecisionRequest(decision="certify", reason="", invoice_id="X",
                                   agent_handle="a@k"), _req(T))
    await db.post_outcome(db.OutcomeEvent(invoice_ref="X", outcome="paid"), _req(T))
    exp = await db.export(_req(T))
    # decisions[] + count byte-identical; reviews[] is the additive Ticket-#3 key
    assert set(exp.keys()) == {"decisions", "count", "reviews"} and exp["count"] == 1
    assert list(exp["decisions"][0].keys()) == _EXPORT_KEYS   # first 10 keys byte-identical, agent_key appended
    assert exp["decisions"][0]["outcome"] == "paid"
    assert isinstance(exp["reviews"], list)


@pytest.mark.asyncio
async def test_scores_route_requires_cgr_read_scope(db):
    T = _tenant()
    with pytest.raises(HTTPException) as ei:
        await db.scores(_req(T, scopes=["decisions:read"]))   # lacks cgr:read and *
    assert ei.value.status_code == 403
    await db.scores(_req(T, scopes=["cgr:read"]))             # granted ⇒ no raise
