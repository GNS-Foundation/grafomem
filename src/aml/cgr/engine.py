"""CGR-v1 engine — load substrate → score per agent → TierGate contract.

Deterministic, no network I/O beyond the injected data-access objects. Reviewer
weights are computed ONCE here (calibration is global across a reviewer's whole
resolved history) and passed into each per-agent `score_agent` call.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from aml.cgr.scoring import (
    CGRResult, DIMENSION_RECEIVABLES, MIN_REVIEWS, _now_iso,
    reviewer_weights, score_agent,
)
from aml.cgr.substrate import DecisionRow, ReviewEvent, load_reviews, load_substrate

# TierGate band contract. Each promotion needs BOTH a score floor and an
# evidence floor (n_resolved) — "report the posterior, not a point". An agent at
# the bare proven floor with a high score resolves to `bronze`, never `gold`.
MIN_RESOLVED_PROVEN = 3          # below this ⇒ "unproven", regardless of score
_TIER_BANDS = (                  # (name, min_score, min_resolved) — first match wins, high→low
    ("gold",   0.80, 20),
    ("silver", 0.65, 10),
    ("bronze", 0.00, MIN_RESOLVED_PROVEN),
)


def compute_scores_from_rows(
    rows: Iterable[DecisionRow],
    *,
    reviews: Iterable[ReviewEvent] = (),
    as_of: str | None = None,
) -> list[CGRResult]:
    """Pure scoring over already-loaded substrate rows (+ optional reviews).

    Groups decisions by agent_handle, computes global reviewer weights from the
    reviews whose invoices are resolved, and scores each agent. Returns results
    sorted by cgr_score descending. Deterministic.
    """
    rows = list(rows)
    reviews = list(reviews)
    as_of = as_of or _now_iso()

    # each row already carries its joined outcome (latest, tenant-scoped)
    outcomes_by_ref = {r.invoice_ref: r.outcome for r in rows
                       if r.invoice_ref is not None and r.outcome is not None}

    # global reviewer calibration: only reviews on RESOLVED invoices inform weight
    resolved_obs = []
    for rv in reviews:
        oc = outcomes_by_ref.get(rv.invoice_ref)
        if oc == "paid" or oc == "default":
            resolved_obs.append((rv.reviewer, rv.rating, 1.0 if oc == "paid" else 0.0))
    rev_w = reviewer_weights(resolved_obs)

    decisions_by_agent: dict[str, list[DecisionRow]] = defaultdict(list)
    reviews_by_agent: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    tier_by_agent: dict[str, float | None] = {}
    for r in rows:
        if r.agent_handle is None:
            continue
        decisions_by_agent[r.agent_handle].append(r)
        if r.agent_tier is not None:            # capability tier (None until TierGate wired)
            tier_by_agent[r.agent_handle] = r.agent_tier
    # Attribute each review to the certifying agent from the JOIN (the decision
    # record is authoritative), falling back to the client-supplied handle. A
    # review whose invoice has no matching decision still informed reviewer_weights
    # above but has no agent to carry its early signal — dropped here, no crash.
    # (Attribution wiring, not scoring math — scoring.py is untouched.)
    agent_by_ref = {r.invoice_ref: r.agent_handle for r in rows if r.invoice_ref is not None}
    for rv in reviews:
        handle = agent_by_ref.get(rv.invoice_ref) or rv.agent_handle
        if handle is None:
            continue
        reviews_by_agent[handle].append((rv.invoice_ref, rv.reviewer, rv.rating))

    results = [
        score_agent(handle, decs, outcomes_by_ref, reviews_by_agent.get(handle, ()),
                    rev_w, tier_by_agent.get(handle), as_of=as_of)
        for handle, decs in decisions_by_agent.items()
    ]
    results.sort(key=lambda r: r.cgr_score, reverse=True)
    return results


def compute_scores(decision_trail, store_manager, tenant_id: str, *,
                   reviews: Iterable[ReviewEvent] | None = None, as_of: str | None = None,
                   limit: int = 500, offset: int = 0) -> list[CGRResult]:
    """Live path: load the tenant's substrate, then score. Reviews are auto-loaded
    from the cgr-reviews store when the caller doesn't supply them (reviews=None);
    pass reviews=() to force the empty (no-review) baseline, or an explicit list."""
    rows = load_substrate(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
    if reviews is None:
        reviews = load_reviews(store_manager, tenant_id)
    return compute_scores_from_rows(rows, reviews=reviews, as_of=as_of)


def to_tiergate(r: CGRResult, *, min_resolved: int = MIN_RESOLVED_PROVEN) -> dict:
    """Map a CGRResult to a TierGate-style band. Contract dict only — this never
    writes to GEIANT/TierGate (cross-repo, out of scope).

    The band is gated on BOTH score and evidence mass: below `min_resolved`
    resolved outcomes an agent is `unproven` (never a confident 0/1); above it,
    each of bronze/silver/gold requires its own score AND n_resolved floor, so a
    thinly-evidenced high score cannot be promoted past bronze.
    """
    if r.n_resolved < min_resolved:
        tier, rationale = "unproven", f"n_resolved={r.n_resolved} < proven floor {min_resolved}"
    else:
        tier, rationale = "bronze", "default band"
        for name, min_score, min_n in _TIER_BANDS:
            if r.cgr_score >= min_score and r.n_resolved >= min_n:
                tier = name
                rationale = f"score {r.cgr_score:.3f} ≥ {min_score} and n_resolved {r.n_resolved} ≥ {min_n}"
                break
    return {
        "agent_handle": r.agent_handle,
        "dimension": r.dimension,
        "tier": tier,
        "cgr_score": r.cgr_score,
        "confidence": r.confidence,
        "n_resolved": r.n_resolved,
        "capability_tier": r.capability_tier,
        "as_of": r.as_of,
        "rationale": rationale,
    }
