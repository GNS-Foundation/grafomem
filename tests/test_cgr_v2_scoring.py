"""CGR #13 — v2 evidence weighting + J-Space capability seam (Parts A + B).

The load-bearing test is the BYTE-IDENTICAL regression: with neutral params
(τ=∞, λ=1, stake=1, no capability profile) v2 reproduces v1 bit-for-bit — proven
against a local copy of the pre-#13 score_agent. The rest exercise each seam.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aml.cgr.engine import compute_scores_from_rows
from aml.cgr.scoring import (
    CEILING_EPS, DEFAULT_REVIEWER_WEIGHT, DIMENSION_RECEIVABLES, K_PRIOR, N_LIFT,
    CapabilityProfile, WeightingConfig, beta_prior, n_lift_for, resolve_capability, score_agent,
)
from aml.cgr.substrate import DecisionRow

AS_OF = "2026-06-01T00:00:00Z"
AS_OF_DT = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _dec(inv: str, decision: str = "certify", tag: str = "judgment") -> SimpleNamespace:
    return SimpleNamespace(decision=decision, verifiability_tag=tag, invoice_ref=inv)


def _erow(inv: str, *, key: str | None = None, outcome: str | None = None,
          tier: float | None = None, outcome_date: datetime | None = None) -> DecisionRow:
    return DecisionRow(decision_id=f"d-{inv}", invoice_ref=inv, agent_handle="a@k",
                       agent_tier=tier, decision="certify", reason_code="clean",
                       verifiability_tag="judgment", created_at=None, outcome=outcome,
                       outcome_date=outcome_date, agent_key=key)


# ---------------------------------------------------------------------------
# The byte-identical v1 reference (exact copy of pre-#13 score_agent math)
# ---------------------------------------------------------------------------

def _v1(decisions, outcomes_by_ref, reviews, reviewer_w, tier, k=K_PRIOR):
    alpha, beta = beta_prior(tier, k)
    n_resolved = 0
    resolved = set()
    for d in decisions:
        if getattr(d, "decision", None) != "certify" or getattr(d, "verifiability_tag", None) != "judgment":
            continue
        ref = getattr(d, "invoice_ref", None)
        oc = outcomes_by_ref.get(ref)
        if oc in ("paid", "default"):
            good = 1.0 if oc == "paid" else 0.0
            alpha += good; beta += 1.0 - good; n_resolved += 1; resolved.add(ref)
    for ref, reviewer, rating in reviews:
        if ref in resolved:
            continue
        w = reviewer_w.get(reviewer, DEFAULT_REVIEWER_WEIGHT)
        alpha += w * float(rating); beta += w * (1.0 - float(rating))
    E = alpha / (alpha + beta)
    if tier is not None:
        s = min(max(n_resolved / N_LIFT, 0.0), 1.0)
        ceiling = float(tier) + CEILING_EPS + (1.0 - float(tier) - CEILING_EPS) * s
        E = min(E, ceiling)
    return E, alpha + beta, n_resolved


def test_v2_neutral_is_byte_identical_to_v1():
    RW = {"good": 0.9, "bad": 0.0}
    scenarios = [
        # (decisions, outcomes, reviews, reviewer_w, tier)
        ([_dec("X")], {}, [], {}, None),                                   # cold-start, no tier
        ([_dec("X"), _dec("Y")], {"X": "paid", "Y": "default"}, [], {}, None),  # resolved mix
        ([_dec("X")], {}, [("X", "good", 0.8), ("X", "bad", 0.2)], RW, None),   # reviews only
        ([_dec(f"I{i}") for i in range(25)],                               # tier + ceiling, high mass
         {f"I{i}": "paid" for i in range(25)}, [], {}, 0.3),
        ([_dec("X"), _dec("Y"), _dec("Z")], {"X": "paid", "Y": "default"},  # mixed resolved+pending+review
         [("Z", "good", 0.7)], RW, 0.9),
    ]
    for decs, outs, revs, rw, tier in scenarios:
        got = score_agent("a", decs, outs, revs, rw, tier, as_of=AS_OF)     # v2, NEUTRAL defaults
        E, n, nr = _v1(decs, outs, revs, rw, tier)
        assert got.cgr_score == E, (got.cgr_score, E)                       # bit-for-bit
        assert got.confidence == n
        assert got.n_resolved == nr


# ---------------------------------------------------------------------------
# Part A — recency, forgetting λ, stake, N_lift
# ---------------------------------------------------------------------------

def test_recency_downweights_stale_good_then_recent_bad():
    decs = [_dec("X"), _dec("Y")]
    outs = {"X": "paid", "Y": "default"}
    odates = {"X": AS_OF_DT - timedelta(days=365), "Y": AS_OF_DT - timedelta(days=1)}
    v1 = score_agent("a", decs, outs, [], {}, None, as_of=AS_OF)            # τ=∞ ⇒ 0.5
    v2 = score_agent("a", decs, outs, [], {}, None, as_of=AS_OF,
                     weighting=WeightingConfig(tau_days=90), outcome_dates_by_ref=odates)
    assert v1.cgr_score == 0.5
    assert v2.cgr_score < v1.cgr_score          # stale good faded, recent bad full ⇒ lower


def test_forgetting_lambda_fades_prior_carryover():
    tier = 0.9                                   # strong good prior (a "pump")
    decs, outs = [_dec("Y")], {"Y": "default"}
    lam1 = score_agent("a", decs, outs, [], {}, tier, as_of=AS_OF)
    lam_half = score_agent("a", decs, outs, [], {}, tier, as_of=AS_OF,
                           weighting=WeightingConfig(lam=0.5))
    assert lam_half.cgr_score < lam1.cgr_score   # faded prior ⇒ the bad outcome pulls it down more
    assert lam_half.confidence < lam1.confidence # λ also fades the prior evidence mass


def test_stake_seam_multiplies_and_default_is_noop():
    decs, outs = [_dec("X")], {"X": "paid"}
    base = score_agent("a", decs, outs, [], {}, None, as_of=AS_OF)
    staked = score_agent("a", decs, outs, [], {}, None, as_of=AS_OF,
                         weighting=WeightingConfig(stake_fn=lambda ref: 2.0))
    assert base.confidence == 3.0 and staked.confidence == 4.0   # stake doubles the observation mass
    assert staked.cgr_score > base.cgr_score
    # default stake_fn (None) is a strict no-op
    assert score_agent("a", decs, outs, [], {}, None, as_of=AS_OF).cgr_score == base.cgr_score


def test_n_lift_ties_to_profile_confidence():
    assert n_lift_for(None) == N_LIFT                 # no profile ⇒ v1 default
    assert n_lift_for(0.9) > n_lift_for(0.1)          # well-measured cap_d ⇒ higher N_lift
    assert n_lift_for(0.0) == round(K_PRIOR + 2)      # noisy cap ⇒ floor ≈ k+2 (proof sooner)


# ---------------------------------------------------------------------------
# Part B — capability profile seam (cap_d source)
# ---------------------------------------------------------------------------

def test_resolve_capability_profile_else_tier_proxy():
    prof = CapabilityProfile(dimension="receivables", cap_d=0.3, issuer="j-space",
                             method="probe", as_of=AS_OF, confidence=0.8)
    assert resolve_capability(prof, 0.7) == (0.3, 0.8)     # profile wins
    assert resolve_capability(None, 0.7) == (0.7, None)    # fall back to tier proxy (v1)
    assert resolve_capability(None, None) == (None, None)


def test_engine_uses_profile_cap_d_when_present_else_tier():
    K = "ab" * 32
    rows = [_erow("X", key=K, outcome="paid", tier=0.5)]
    # absent ⇒ cap_d = agent_tier proxy (v1)
    assert compute_scores_from_rows(rows)[0].capability_tier == 0.5
    # present ⇒ cap_d = profile value (drives prior + ceiling)
    prof = {K: CapabilityProfile("receivables", 0.3, "j-space", "probe", AS_OF)}
    r = compute_scores_from_rows(rows, capability_profiles=prof)[0]
    assert r.capability_tier == 0.3


def test_engine_profile_lookup_by_subject_did():
    from aml.cgr.identity import did_key
    K = "cd" * 32
    rows = [_erow("X", key=K, outcome="paid")]
    prof = {did_key(K): CapabilityProfile("receivables", 0.25, "j-space", "probe", AS_OF)}
    r = compute_scores_from_rows(rows, capability_profiles=prof)[0]
    assert r.capability_tier == 0.25          # resolved via the anchor's did:key


def test_evidence_gated_ceiling_preserved_with_profile_cap_d():
    # cap_d = 0.3 but 25 proven-good outcomes ⇒ ceiling LIFTS (no hard clamp at 0.32)
    K = "ab" * 32
    rows = [_erow(f"I{i}", key=K, outcome="paid") for i in range(25)]
    prof = {K: CapabilityProfile("receivables", 0.3, "j-space", "probe", AS_OF)}
    r = compute_scores_from_rows(rows, capability_profiles=prof)[0]
    assert r.capability_tier == 0.3
    assert r.n_resolved == 25
    assert r.cgr_score > 0.3 + CEILING_EPS + 0.3    # far above cap_d+ε ⇒ verifiable mass dominated
