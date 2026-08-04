"""CGR-v1 scoring math — pure functions, no I/O (numpy + stdlib only).

Faithful port of the validated reference (`docs/cgr/cgr_substrate_reference.py`,
which reached corr −0.99 vs realized default on synthetic receivables data). The
core is a per-agent Beta-mean trust score:

  * PRIOR from the GEIANT capability tier:  α = 1 + k·tier,  β = 1 + k·(1−tier).
    tier is None (current POC — TierGate not wired) ⇒ neutral prior α = β = 1.
  * CALIBRATION on resolved outcomes (the verifiable slice): each resolved
    `certify`+`judgment` decision updates Beta at full weight — paid → α, default → β.
  * EARLY signal on still-unresolved certifies: a reviewer-reliability-weighted
    soft count (α += w·rating, β += w·(1−rating)), where w comes from Brier
    calibration of each reviewer on *resolved* invoices ("verify the reviewer").
  * SCORE  E = α/(α+β);  confidence n = α+β (evidence mass).
  * CEILING (Ticket #2 addition, not in the reference) — evidence-gated: tight
    when verifiable evidence is thin (guards cold-start / review-farm inflation),
    lifts as resolved outcomes prove capability (verifiable evidence dominates).
    Skipped entirely when tier is None. The gate ramps the ceiling from
    tier + CEILING_EPS (at n_resolved = 0) to 1.0 (at n_resolved ≥ N_LIFT), so a
    low-tier agent cannot be inflated past its tier on thin/soft evidence, but a
    genuinely capable agent with proven outcomes is not held down by a noisy tier.
    (Roadmap: `tier` should eventually be a J-Space capability measurement from
    the neutral authority, not the TierGate proxy — the evidence gate is what
    makes the proxy safe to use in the meantime.)

Only `certify` decisions tagged `judgment` earn credit/blame; rule-rejects are
excluded (they are deterministic, not a judgment call).

v2 (Ticket #13) WIRES the deferred knobs behind a `WeightingConfig` whose defaults
are NEUTRAL, so v1 output is reproduced BYTE-FOR-BYTE (regression-tested):
  * recency_i = exp(-Δt/τ) per observation (τ=∞ default ⇒ 1);
  * forgetting factor λ on the prior carry-over (λ=1 default);
  * stake_i injection seam (default 1 — no staking source exists yet, not fabricated);
  * N_lift on the evidence-gated ceiling is now configurable (ceiling formula
    UNCHANGED) and can tie to a J-Space capability profile's confidence (Part B).
The capability signal `cap_d` resolves from a `CapabilityProfile` when the neutral
measurement authority has issued one for the agent's identity, else the TierGate
proxy exactly as v1 (`resolve_capability`). The measurement instrument that PRODUCES
a profile is out of scope (Part C, flagged/separate).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import numpy as np

# Dimension axis — v1 is receivables-only. Carried on every result and threaded
# through scoring so adding dimensions later is additive, not a rewrite. No
# cross-domain logic exists yet; this is the field only.
DIMENSION_RECEIVABLES = "receivables"

K_PRIOR = 4.0                    # capability-prior strength (reference K_PRIOR)
CEILING_EPS = 0.02               # thin-evidence ceiling margin above tier (n_resolved=0)
N_LIFT = 20                      # resolved outcomes at which the ceiling fully lifts to 1.0
MIN_REVIEWS = 5                  # reviewer needs ≥ this many resolved obs to earn a real weight
DEFAULT_REVIEWER_WEIGHT = 0.05   # floor weight for under-observed / unseen reviewers
BRIER_SCALE = 0.25               # worst-case Brier for a [0,1] estimate (normalizer)


@dataclass(frozen=True)
class CGRResult:
    """One agent's capability-grounded reputation, for one dimension."""
    agent_handle: str
    cgr_score: float            # posterior Beta mean E = α/(α+β), tier-capped
    confidence: float           # evidence mass n = α+β (prior + observed)
    n_resolved: int             # resolved judgment-certifies that updated Beta
    n_pending: int              # judgment-certifies still awaiting an outcome
    capability_tier: float | None
    as_of: str                  # ISO-8601 UTC timestamp of the computation
    dimension: str = DIMENSION_RECEIVABLES
    # CGR identity binding (Ticket #5): the bound GEIANT pubkey this reputation is
    # for, or None when the agent's decisions carried no key. Data only — set by
    # engine.compute_scores via dataclasses.replace; the Beta math never reads it.
    subject_key: str | None = None
    # CGR identity continuity (Ticket #7): did:key of the IDENTITY ANCHOR (genesis
    # key). == did:key(subject_key) when no rotation has occurred; after a rotation
    # subject_key is the current operational key while subject_did stays the anchor.
    subject_did: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WeightingConfig:
    """v2 evidence-weighting knobs (Ticket #13 Part A). Every default is NEUTRAL, so
    with the defaults the score is BYTE-IDENTICAL to v1. See the math in
    docs/cgr/reputation-score-design.md ("The math").

    The intended split (avoid double-decay): `lam` fades the accumulated/prior mass
    (a one-time pump decays); `recency` weights each NEW observation by its age.
    Recency-on-evidence is the recommended primary knob with `lam=1`."""
    tau_days: float | None = None        # recency scale: recency_i = exp(-Δt/τ); None ⇒ 1.0 (v1)
    lam: float = 1.0                      # forgetting factor λ ∈ (0,1] on the prior carry-over
    n_lift: int = N_LIFT                  # evidence-gate lift point (design: ≈ k+2, adjusted for cap noise)
    stake_fn: Callable[[str], float] | None = None   # (ref) -> stake_i; None ⇒ 1.0 (no source yet — do not fabricate)


DEFAULT_WEIGHTING = WeightingConfig()


@dataclass(frozen=True)
class CapabilityProfile:
    """Minimal, versioned J-Space capability profile (Ticket #13 Part B) — the
    `cap_d` source. Produced by a neutral measurement AUTHORITY; the instrument that
    produces it is NOT built here (Part C, flagged/separate). Consumed as `cap_d` in
    the Beta prior and the evidence-gated ceiling, in place of the TierGate proxy."""
    dimension: str
    cap_d: float                         # ∈ [0,1] verified capability ceiling
    issuer: str                          # measurement authority / body (provenance)
    method: str                          # how cap_d was measured (provenance)
    as_of: str                           # ISO-8601 (provenance)
    confidence: float | None = None      # optional ∈ [0,1]: how well-measured cap_d is
    schema: str = "cgr.capability.v1"


def resolve_capability(profile: "CapabilityProfile | None",
                       agent_tier: float | None) -> tuple[float | None, float | None]:
    """Resolve the capability signal `cap_d`: from the J-Space profile when one
    exists for the agent's identity, ELSE the TierGate proxy (`agent_tier`) exactly
    as v1. Returns (cap_d, confidence)."""
    if profile is not None:
        conf = None if profile.confidence is None else float(profile.confidence)
        return float(profile.cap_d), conf
    return agent_tier, None


def n_lift_for(confidence: float | None, base: int = N_LIFT, k: float = K_PRIOR) -> int:
    """Evidence-gate lift point N_lift. Default `base` (= N_LIFT) when there is no
    profile confidence — v1 behaviour. With a profile confidence, interpolate from a
    floor (≈ k+2, the design's "verifiable mass overwhelms prior mass" point) up to
    `base` by confidence: a WELL-measured cap_d (high confidence) is relied on longer
    ⇒ higher N_lift; a NOISY cap_d (low confidence) lifts the ceiling sooner ⇒ lower
    N_lift. (docs/cgr/reputation-score-design.md, §N_lift calibration.)"""
    if confidence is None:
        return int(base)
    floor = int(round(k + 2))
    conf = min(max(float(confidence), 0.0), 1.0)
    return max(floor, int(round(floor + (int(base) - floor) * conf)))


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_as_of_dt(as_of: str | None) -> datetime | None:
    if not as_of:
        return None
    try:
        return _as_utc(datetime.fromisoformat(as_of.replace("Z", "+00:00")))
    except ValueError:
        return None


def _recency(ts: datetime | None, as_of_dt: datetime | None, tau_days: float | None) -> float:
    """Per-observation recency weight exp(-Δt/τ), Δt = age (days) from `ts` to `as_of`.
    τ=None (default) ⇒ 1.0 (no decay, v1). A future/missing ts ⇒ full weight."""
    if tau_days is None or ts is None or as_of_dt is None:
        return 1.0
    dt_days = (as_of_dt - _as_utc(ts)).total_seconds() / 86400.0
    if dt_days <= 0.0:
        return 1.0
    return float(np.exp(-dt_days / float(tau_days)))


def beta_prior(tier: float | None, k: float = K_PRIOR) -> tuple[float, float]:
    """Capability prior as Beta(α, β). tier is None ⇒ neutral prior (1, 1)."""
    if tier is None:
        return 1.0, 1.0
    t = float(tier)
    return 1.0 + k * t, 1.0 + k * (1.0 - t)


def reviewer_weights(resolved_obs: Iterable[tuple[str, float, float]]) -> dict[str, float]:
    """Brier-calibrated reliability weight per reviewer, from *resolved* reviews.

    resolved_obs: iterable of (reviewer_handle, rating∈[0,1], good∈{0.0,1.0}) where
    good = 1 if the reviewed invoice was later paid, 0 if it defaulted. A reviewer
    with ≥ MIN_REVIEWS observations gets w = clip(1 − mean_sq_err / 0.25, 0, 1);
    fewer observations get the floor weight. Empty input ⇒ {} (no review data).
    """
    errs: dict[str, list[float]] = {}
    for reviewer, rating, good in resolved_obs:
        errs.setdefault(reviewer, []).append((float(rating) - float(good)) ** 2)
    weights: dict[str, float] = {}
    for reviewer, sq in errs.items():
        if len(sq) >= MIN_REVIEWS:
            weights[reviewer] = float(np.clip(1.0 - (sum(sq) / len(sq)) / BRIER_SCALE, 0.0, 1.0))
        else:
            weights[reviewer] = DEFAULT_REVIEWER_WEIGHT
    return weights


def score_agent(
    agent_handle: str,
    decisions: Iterable,
    outcomes_by_ref: dict[str, str],
    reviews: Iterable[tuple],
    reviewer_w: dict[str, float],
    tier: float | None,
    *,
    k: float = K_PRIOR,
    as_of: str | None = None,
    dimension: str = DIMENSION_RECEIVABLES,
    weighting: WeightingConfig = DEFAULT_WEIGHTING,
    outcome_dates_by_ref: dict | None = None,
) -> CGRResult:
    """Score one agent from its decisions + the joined outcomes + reviews.

    decisions        : this agent's DecisionRow-like items (need .decision,
                       .verifiability_tag, .invoice_ref).
    outcomes_by_ref  : invoice_ref -> outcome string ("paid"|"default"|...).
    reviews          : this agent's (invoice_ref, reviewer_handle, rating) tuples,
                       optionally a 4th element (review_date) for recency.
    reviewer_w       : global reviewer weights (computed once by the engine).
    tier             : the capability signal cap_d ∈ [0,1] (a J-Space profile value
                       or the TierGate proxy) used in the prior + evidence-gated
                       ceiling, or None (neutral prior, no ceiling).
    weighting        : v2 knobs (recency τ, forgetting λ, N_lift, stake). Defaults
                       are NEUTRAL ⇒ output byte-identical to v1.
    outcome_dates_by_ref : invoice_ref -> outcome timestamp, for recency of the
                       verifiable slice (only consulted when weighting.tau_days set).

    v2 evidence weight (docs/cgr/reputation-score-design.md):
        w_i = verifiability_i × calibration_i × stake_i × recency_i
        α ← λ·α_prior + Σ w_i·r_i ;  β ← λ·β_prior + Σ w_i·(1−r_i)
    """
    lam = weighting.lam
    tau = weighting.tau_days
    stake_fn = weighting.stake_fn or (lambda _ref: 1.0)
    as_of_dt = _parse_as_of_dt(as_of) if tau is not None else None
    outcome_dates = outcome_dates_by_ref or {}

    # λ fades the accumulated/prior mass (a one-time pump decays). λ=1 ⇒ v1.
    alpha_prior, beta_prior_ = beta_prior(tier, k)
    alpha = lam * alpha_prior
    beta = lam * beta_prior_
    n_resolved = n_pending = 0
    resolved_refs: set[str] = set()

    for d in decisions:
        # only agent-posted judgment certifications earn credit/blame
        if getattr(d, "decision", None) != "certify" or getattr(d, "verifiability_tag", None) != "judgment":
            continue
        ref = getattr(d, "invoice_ref", None)
        outcome = outcomes_by_ref.get(ref)
        if outcome == "paid" or outcome == "default":
            good = 1.0 if outcome == "paid" else 0.0
            # verifiable ground truth: verifiability=1, calibration=1; × stake × recency
            w = float(stake_fn(ref)) * _recency(outcome_dates.get(ref), as_of_dt, tau)
            alpha += w * good
            beta += w * (1.0 - good)
            n_resolved += 1
            resolved_refs.add(ref)
        else:
            n_pending += 1

    # reviewer-weighted early signal on UNRESOLVED certifications only
    for review in reviews:
        ref, reviewer, rating = review[0], review[1], review[2]
        if ref in resolved_refs:
            continue
        review_date = review[3] if len(review) > 3 else None
        # feedback: calibration = reviewer weight (verifiability folded in); × stake × recency
        w = (reviewer_w.get(reviewer, DEFAULT_REVIEWER_WEIGHT)
             * float(stake_fn(ref)) * _recency(review_date, as_of_dt, tau))
        alpha += w * float(rating)
        beta += w * (1.0 - float(rating))

    E = alpha / (alpha + beta)
    # capability ceiling — evidence-gated: tight when verifiable evidence is thin
    # (guards cold-start / review-farm inflation), lifts as resolved outcomes prove
    # capability (verifiable evidence dominates). Skipped entirely when tier is None.
    # N_lift is configurable (weighting.n_lift); the formula is UNCHANGED from v1.
    if tier is not None:
        nl = max(int(weighting.n_lift), 1)
        s = min(max(n_resolved / nl, 0.0), 1.0)
        ceiling = float(tier) + CEILING_EPS + (1.0 - float(tier) - CEILING_EPS) * s
        E = min(E, ceiling)

    return CGRResult(
        agent_handle=agent_handle,
        cgr_score=float(E),
        confidence=float(alpha + beta),
        n_resolved=n_resolved,
        n_pending=n_pending,
        capability_tier=(None if tier is None else float(tier)),
        as_of=as_of or _now_iso(),
        dimension=dimension,
    )
