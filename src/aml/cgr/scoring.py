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

DELIBERATE v1 OMISSIONS (flagged for v2 — see reputation-score-design.md):
  * Forgetting factor λ (time decay of old evidence): the reference and v1 weight
    all resolved outcomes equally. Decay matters for anti-pump once an agent has
    volume (stale good behavior shouldn't mask a recent turn) — deferred.
  * Stake-weighted evidence (invoice amount / capital at risk): v1 counts each
    outcome as 1. Needs an amount field carried into scoring — substrate has it
    on decisions but v1 does not consume it.
  * Recency-weighted reviewer calibration: v1 uses a flat Brier over a reviewer's
    whole resolved history.
All three need substrate we either don't capture yet (stake/recency provenance)
or additional design (λ tuning); v1 follows the validated reference exactly so
the −0.7 field correlation is attributable to the ported core, not new knobs.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    reviews: Iterable[tuple[str, str, float]],
    reviewer_w: dict[str, float],
    tier: float | None,
    *,
    k: float = K_PRIOR,
    as_of: str | None = None,
    dimension: str = DIMENSION_RECEIVABLES,
) -> CGRResult:
    """Score one agent from its decisions + the joined outcomes + reviews.

    decisions        : this agent's DecisionRow-like items (need .decision,
                       .verifiability_tag, .invoice_ref).
    outcomes_by_ref  : invoice_ref -> outcome string ("paid"|"default"|...).
    reviews          : this agent's (invoice_ref, reviewer_handle, rating) tuples.
    reviewer_w       : global reviewer weights (computed once by the engine).
    tier             : capability tier in [0,1] or None (neutral prior, no ceiling).
    """
    alpha, beta = beta_prior(tier, k)
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
            alpha += good
            beta += 1.0 - good
            n_resolved += 1
            resolved_refs.add(ref)
        else:
            n_pending += 1

    # reviewer-weighted early signal on UNRESOLVED certifications only
    for ref, reviewer, rating in reviews:
        if ref in resolved_refs:
            continue
        w = reviewer_w.get(reviewer, DEFAULT_REVIEWER_WEIGHT)
        alpha += w * float(rating)
        beta += w * (1.0 - float(rating))

    E = alpha / (alpha + beta)
    # capability ceiling — evidence-gated: tight when verifiable evidence is thin
    # (guards cold-start / review-farm inflation), lifts as resolved outcomes prove
    # capability (verifiable evidence dominates). Skipped entirely when tier is None.
    if tier is not None:
        s = min(max(n_resolved / N_LIFT, 0.0), 1.0)
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
