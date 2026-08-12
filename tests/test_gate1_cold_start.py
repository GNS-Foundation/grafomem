"""B2b Gate-1 — offline cold-start gate (deterministic, no network, no DB).

Mirrors B2a's offline fraud gate: seeds a farmed thin target and asserts the
review-channel calibration gate floors Sybil inflation while leaving legitimate,
calibrated reviewers and the verifiable channel untouched. All logic under test lives
in grafomem CGR (scoring.py seam + gate.py); the meridian sim is never imported.

Six checks + a divergence (moat) test + the §6.5 newcomer-exclusion metric.
"""
from __future__ import annotations

from types import SimpleNamespace

from aml.cgr.scoring import CGRResult, WeightingConfig, score_agent
from aml.cgr.gate import build_review_gate, review_gate_g, newcomer_exclusion_pct

TAU = 0.10   # locked B2b operating point (§8): soft ramp opens just above the floor
CAP_K = 3.0


def _cert(ref: str):
    """A judgment certification (the only decision kind that earns credit/blame)."""
    return SimpleNamespace(decision="certify", verifiability_tag="judgment", invoice_ref=ref)


def _score(handle, decisions, outcomes, reviews, reviewer_w, *, calibration=None, tier=None):
    """Score once. calibration=None ⇒ NEUTRAL (no gate) — the 'local stub' path."""
    if calibration is None:
        weighting = WeightingConfig()  # v1 / ungated
    else:
        weighting = WeightingConfig(
            review_gate=build_review_gate(calibration, TAU),
            review_cap_k=CAP_K,
        )
    return score_agent(handle, decisions, outcomes, reviews, reviewer_w, tier,
                       as_of="2026-08-12T00:00:00+00:00", weighting=weighting)


def _review_mass(r: CGRResult) -> float:
    """Pseudo-count mass contributed by reviews = confidence − the (1+1) neutral prior."""
    return r.confidence - 2.0


# ── 1. Sybil floor: a farm of below-τ / unknown sources cannot lift a thin target ──
def test_1_sybil_review_farm_is_floored():
    refs = [f"inv-{i}" for i in range(12)]
    farm = [f"sybil-{i}" for i in range(12)]
    reviews = [(refs[i], farm[i], 1.0) for i in range(12)]        # all rate the target perfect
    reviewer_w = {r: 1.0 for r in farm}                            # even with full reviewer weight…
    cal = {r: 0.05 for r in farm}                                  # …their calibration w < τ ⇒ g=0
    gated = _score("target", [_cert(r) for r in refs], {}, reviews, reviewer_w, calibration=cal)
    ungated = _score("target", [_cert(r) for r in refs], {}, reviews, reviewer_w)
    assert abs(gated.cgr_score - 0.5) < 1e-9, "gated thin target must stay at the neutral prior"
    assert ungated.cgr_score > 0.8, "without the gate the farm inflates the target (sanity)"


# ── 2. Legitimate pass-through: a calibrated (w>τ) source still contributes ──
def test_2_calibrated_source_passes_through():
    reviews = [("inv-0", "legit", 1.0)]
    reviewer_w = {"legit": 1.0}
    cal = {"legit": 0.8}                                           # g(0.8) = (0.8-0.3)/0.7 ≈ 0.714
    gated = _score("t", [_cert("inv-0")], {}, reviews, reviewer_w, calibration=cal)
    ungated = _score("t", [_cert("inv-0")], {}, reviews, reviewer_w)
    assert gated.cgr_score > 0.5, "a calibrated positive reviewer must move the score up"
    assert gated.cgr_score < ungated.cgr_score, "g(w)<1 damps it below the ungated value"
    assert abs(_review_mass(gated) - review_gate_g(0.8, TAU) * 1.0) < 1e-9


# ── 3. Cold-start fail-safe: an unknown source (no calibration row) contributes 0 ──
def test_3_unknown_source_fails_safe_to_zero():
    reviews = [("inv-0", "newcomer", 1.0)]
    reviewer_w = {"newcomer": 1.0}
    gated = _score("t", [_cert("inv-0")], {}, reviews, reviewer_w, calibration={})  # not in map
    assert abs(gated.cgr_score - 0.5) < 1e-9
    assert abs(_review_mass(gated)) < 1e-12


# ── 4. Cap bound: one source's total contribution to ONE target is ≤ K ──
def test_4_per_source_cap_bounds_single_target():
    refs = [f"inv-{i}" for i in range(20)]
    reviews = [(r, "loud", 1.0) for r in refs]                     # 20 reviews from one source
    reviewer_w = {"loud": 1.0}
    cal = {"loud": 1.0}                                            # g=1, so only the cap bounds it
    gated = _score("t", [_cert(r) for r in refs], {}, reviews, reviewer_w, calibration=cal)
    assert _review_mass(gated) <= CAP_K + 1e-9, "a single source cannot exceed K on one target"
    ungated = _score("t", [_cert(r) for r in refs], {}, reviews, reviewer_w)
    assert _review_mass(ungated) > CAP_K, "without the cap the mass blows past K (sanity)"


# ── 5. Per-(source,target): the cap is NOT a global reviewer budget ──
def test_5_cap_is_per_source_target_not_global():
    refs_a = [f"a-{i}" for i in range(20)]
    refs_b = [f"b-{i}" for i in range(20)]
    reviewer_w = {"loud": 1.0}
    cal = {"loud": 1.0}
    a = _score("tA", [_cert(r) for r in refs_a], {}, [(r, "loud", 1.0) for r in refs_a], reviewer_w, calibration=cal)
    b = _score("tB", [_cert(r) for r in refs_b], {}, [(r, "loud", 1.0) for r in refs_b], reviewer_w, calibration=cal)
    # the same source contributes up to K to EACH distinct target (independent caps)
    assert abs(_review_mass(a) - CAP_K) < 1e-9
    assert abs(_review_mass(b) - CAP_K) < 1e-9


# ── 6. Verifiable channel invariant: real resolved outcomes score identically pre/post gate ──
def test_6_verifiable_channel_is_never_gated():
    refs = [f"inv-{i}" for i in range(8)]
    outcomes = {r: ("paid" if i % 2 == 0 else "default") for i, r in enumerate(refs)}
    decisions = [_cert(r) for r in refs]
    gated = _score("t", decisions, outcomes, [], {}, calibration={"anyone": 0.9})
    ungated = _score("t", decisions, outcomes, [], {})
    assert gated.cgr_score == ungated.cgr_score, "gate must not touch the verifiable channel"
    assert gated.confidence == ungated.confidence and gated.n_resolved == 8


# ── divergence (moat): a local re-implementation without the gate diverges from grafomem ──
def test_divergence_moat():
    refs = [f"inv-{i}" for i in range(12)]
    farm = [f"sybil-{i}" for i in range(12)]
    reviews = [(refs[i], farm[i], 1.0) for i in range(12)]
    reviewer_w = {r: 1.0 for r in farm}
    cal = {r: 0.05 for r in farm}
    grafomem = _score("target", [_cert(r) for r in refs], {}, reviews, reviewer_w, calibration=cal)  # gated
    local_stub = _score("target", [_cert(r) for r in refs], {}, reviews, reviewer_w)                 # no gate
    # On the farmed thin target the local stub inflates while grafomem floors it → they DIVERGE.
    assert grafomem.cgr_score < local_stub.cgr_score - 0.3, "gate must be inside the moat (local ≠ grafomem)"


# ── §6.5 newcomer-exclusion metric on the locked mixed w-distribution (τ=0.10) ──
def test_6_5_newcomer_exclusion_metric():
    # Operating-point population: ~70% newcomers Beta(1.5,4) (low, cold-start) + ~30%
    # established Beta(5,2) (high). Report the fraction fully excluded (g(w)=0) at τ=0.10.
    import numpy as np
    rng = np.random.default_rng(42)  # deterministic
    n = 4000
    n_new = int(0.70 * n)
    w = np.concatenate([
        rng.beta(1.5, 4.0, size=n_new),        # newcomers (mean ≈ 0.27, long low tail)
        rng.beta(5.0, 2.0, size=n - n_new),    # established (mean ≈ 0.71)
    ])
    cal = {f"src{i}": float(w[i]) for i in range(n)}
    sources = [f"src{i}" for i in range(n)]
    pct = newcomer_exclusion_pct(cal, sources, TAU)
    print(f"\n[§6.5] newcomer-exclusion at τ={TAU} on 70%·Beta(1.5,4)+30%·Beta(5,2): {pct * 100:.1f}%")
    # The excluded set is the low tail of the newcomer component (established mass sits
    # well above τ). Expect ~15–20% — a soft floor, NOT the τ=0.30 clamp (~70%).
    assert 0.10 <= pct <= 0.22, f"expected ~15–20% exclusion at τ={TAU}, got {pct * 100:.1f}%"
