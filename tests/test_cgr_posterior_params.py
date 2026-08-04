"""CGR #8a — Beta posterior + capability provenance on the scores payload.

The GET /v1/cgr/scores serializer is literally `asdict(CGRResult)`, so these
engine-level tests assert on `asdict(compute_scores_from_rows(...)[0])` — the exact
dict the route returns. (A true end-to-end route test lives in test_cgr_scoring.py,
reusing its Postgres `db` fixture.) Additive + backward-compatible: no scoring-math
change; the byte-identical v1 regression continues to hold in test_cgr_v2_scoring.
"""
from __future__ import annotations

from dataclasses import asdict

from aml.cgr.engine import compute_scores_from_rows
from aml.cgr.scoring import CapabilityProfile
from aml.cgr.substrate import DecisionRow

AS_OF = "2026-06-01T00:00:00Z"
K = "ab" * 32


def _row(inv, *, key=None, outcome=None, tier=None):
    return DecisionRow(decision_id=f"d-{inv}", invoice_ref=inv, agent_handle="a@k",
                       agent_tier=tier, decision="certify", reason_code="clean",
                       verifiability_tag="judgment", created_at=None, outcome=outcome,
                       outcome_date=None, agent_key=key)


def test_payload_carries_posterior_matching_mean_and_mass():
    # tier None ⇒ no ceiling ⇒ cgr_score == α/(α+β) exactly; one paid + one default.
    rows = [_row("X", outcome="paid"), _row("Y", outcome="default")]
    d = asdict(compute_scores_from_rows(rows, as_of=AS_OF)[0])
    for f in ("post_alpha", "post_beta", "cap_d", "cap_source", "cap_confidence"):
        assert f in d                                   # additive fields present on the payload
    a, b = d["post_alpha"], d["post_beta"]
    assert a == 2.0 and b == 2.0                        # neutral prior (1,1) + paid→α, default→β
    assert abs(a / (a + b) - d["cgr_score"]) < 1e-12    # posterior mean == score (ceiling unbound)
    assert abs((a + b) - d["confidence"]) < 1e-12       # α+β == confidence (evidence mass)


def test_cap_source_tier_proxy_when_no_profile():
    d = asdict(compute_scores_from_rows([_row("X", outcome="paid", tier=0.5)], as_of=AS_OF)[0])
    assert d["cap_source"] == "tier_proxy"              # TierGate fallback, no J-Space profile
    assert d["cap_d"] == 0.5                            # cap_d == the tier used for prior/ceiling
    assert d["cap_confidence"] is None


def test_cap_source_profile_and_confidence_surface():
    rows = [_row("X", key=K, outcome="paid")]
    prof = {K: CapabilityProfile("receivables", 0.3, "j-space", "probe", AS_OF, confidence=0.8)}
    d = asdict(compute_scores_from_rows(rows, capability_profiles=prof, as_of=AS_OF)[0])
    assert d["cap_source"] == "profile"                 # cap_d came from a CapabilityProfile
    assert d["cap_d"] == 0.3                            # the profile's measured value
    assert d["cap_confidence"] == 0.8                   # its confidence surfaces


def test_pre_8a_direct_score_agent_still_serializes():
    # A CGRResult built off the engine path (no cap_source stamp) must still serialize;
    # score_agent fills the posterior + cap_d, engine-only fields default to None.
    from aml.cgr.scoring import score_agent
    r = score_agent("a@k", [], {}, [], {}, None, as_of=AS_OF)   # cold start, no engine
    d = asdict(r)
    assert d["post_alpha"] == 1.0 and d["post_beta"] == 1.0     # neutral prior surfaced
    assert d["cap_d"] is None and d["cap_source"] is None       # nullable, no crash
