"""CGR-v1 engine — load substrate → score per agent → TierGate contract.

Deterministic, no network I/O beyond the injected data-access objects. Reviewer
weights are computed ONCE here (calibration is global across a reviewer's whole
resolved history) and passed into each per-agent `score_agent` call.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace

from aml.cgr.identity import did_key, resolve_identities
from aml.cgr.scoring import (
    CGRResult, DEFAULT_WEIGHTING, DIMENSION_RECEIVABLES, MIN_REVIEWS, WeightingConfig,
    _now_iso, n_lift_for, resolve_capability, reviewer_weights, score_agent,
)
from aml.cgr.substrate import (
    DecisionRow, ReviewEvent, load_reviews, load_rotations, load_substrate,
)

logger = logging.getLogger("grafomem.cgr.engine")

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
    rotations: Iterable = (),
    verify=None,
    as_of: str | None = None,
    weighting: "WeightingConfig | None" = None,
    capability_profiles: dict | None = None,
) -> list[CGRResult]:
    """Pure scoring over already-loaded substrate rows (+ optional reviews + rotations).

    Groups decisions by IDENTITY ANCHOR (Ticket #7) — an agent_key is folded to its
    anchor via the verified rotation chain, so a rotated agent's whole key-history
    rolls up to one result — falling back to agent_key then agent_handle (legacy).
    Computes global reviewer weights and scores each agent. Deterministic.

    `rotations` + `verify` are injected; with either absent, aggregation is by
    agent_key/agent_handle exactly as #5 (backward-compatible). `verify(pubkey_hex,
    message, sig_hex) -> bool` is the Ed25519 capability, kept out of this pure path.
    """
    rows = list(rows)
    reviews = list(reviews)
    rotations = list(rotations)
    as_of = as_of or _now_iso()
    weighting = weighting or DEFAULT_WEIGHTING   # neutral ⇒ byte-identical to v1

    # Resolve rotation chains → {op_key: anchor}, {anchor: current_key}, frozen set.
    if rotations and verify is not None:
        anchor_of, current_of, _frozen = resolve_identities(rotations, verify=verify)
    else:
        anchor_of, current_of = {}, {}

    # each row already carries its joined outcome (latest, tenant-scoped)
    outcomes_by_ref = {r.invoice_ref: r.outcome for r in rows
                       if r.invoice_ref is not None and r.outcome is not None}
    # outcome timestamps for recency (v2) — only consulted when weighting.tau_days is set
    outcome_dates_by_ref = {r.invoice_ref: r.outcome_date for r in rows
                            if r.invoice_ref is not None and r.outcome_date is not None}

    # global reviewer calibration: only reviews on RESOLVED invoices inform weight
    resolved_obs = []
    for rv in reviews:
        oc = outcomes_by_ref.get(rv.invoice_ref)
        if oc == "paid" or oc == "default":
            resolved_obs.append((rv.reviewer, rv.rating, 1.0 if oc == "paid" else 0.0))
    rev_w = reviewer_weights(resolved_obs)

    # Aggregation key: the IDENTITY ANCHOR of the agent's captured GEIANT pubkey
    # (Ticket #7 — folds a rotated key back to its genesis), else the pubkey itself
    # (#5), else the handle (legacy). The handle is a label; the key is the identity;
    # the anchor is the identity across rotation. Never back-resolve key↔handle.
    def _gkey(r: DecisionRow) -> str | None:
        if r.agent_key:
            return anchor_of.get(r.agent_key, r.agent_key)   # anchor, or the key if never rotated
        return r.agent_handle

    decisions_by_agent: dict[str, list[DecisionRow]] = defaultdict(list)
    reviews_by_agent: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    tier_by_agent: dict[str, float | None] = {}
    label_by_agent: dict[str, str | None] = {}     # gkey -> human handle label
    subject_by_agent: dict[str, str | None] = {}   # gkey -> current operational pubkey, or None (legacy)
    did_by_agent: dict[str, str | None] = {}       # gkey -> anchor did:key, or None (legacy)
    for r in rows:
        g = _gkey(r)
        if g is None:
            continue
        decisions_by_agent[g].append(r)
        label_by_agent.setdefault(g, r.agent_handle)
        if r.agent_key:
            # g is the identity anchor: subject_key = current op key (== g if never
            # rotated), subject_did = anchor did:key (stable across rotation).
            subject_by_agent[g] = current_of.get(g, g)
            did_by_agent[g] = did_key(g)
        else:
            subject_by_agent.setdefault(g, None)
            did_by_agent.setdefault(g, None)
        if r.agent_tier is not None:            # capability tier (None until TierGate wired)
            tier_by_agent[g] = r.agent_tier
    # Attribute each review to the certifying agent from the JOIN (the decision
    # record is authoritative), falling back to the client-supplied handle. A
    # review whose invoice has no matching decision still informed reviewer_weights
    # above but has no agent to carry its early signal — dropped here, no crash.
    # (Attribution wiring, not scoring math — scoring.py is untouched.)
    agent_by_ref = {r.invoice_ref: _gkey(r) for r in rows if r.invoice_ref is not None}
    orphans = sorted({rv.invoice_ref for rv in reviews if rv.invoice_ref not in agent_by_ref})
    if orphans:
        # A review referencing an invoice with no captured decision is a capture gap
        # against Ticket-#1's spine (mirrors the null-invoice_ref decision warning):
        # it can inform neither calibration nor attribution and is dropped from scoring.
        logger.warning("CGR: %d review(s) reference invoice_refs with no captured decision "
                       "(capture gap — not scored): %s%s", len(orphans), orphans[:5],
                       " …" if len(orphans) > 5 else "")
    for rv in reviews:
        handle = agent_by_ref.get(rv.invoice_ref) or rv.agent_handle
        if handle is None:
            continue
        # 4-tuple carries the review timestamp for recency (v2); None ⇒ recency 1
        reviews_by_agent[handle].append(
            (rv.invoice_ref, rv.reviewer, rv.rating, getattr(rv, "review_date", None)))

    results = []
    for g, decs in decisions_by_agent.items():
        subject_did = did_by_agent.get(g)
        # Part B: resolve cap_d from a J-Space capability profile keyed by the agent's
        # identity (anchor key or its did:key), else the TierGate proxy exactly as v1.
        profile = None
        if capability_profiles:
            profile = capability_profiles.get(g) or (
                capability_profiles.get(subject_did) if subject_did else None)
        cap_d, cap_conf = resolve_capability(profile, tier_by_agent.get(g))
        # A well-measured cap_d (high confidence) is relied on longer (higher N_lift);
        # no profile ⇒ N_lift unchanged. Ceiling formula itself is untouched.
        agent_weighting = replace(weighting, n_lift=n_lift_for(cap_conf, weighting.n_lift))
        res = score_agent(
            label_by_agent[g], decs, outcomes_by_ref, reviews_by_agent.get(g, ()),
            rev_w, cap_d, as_of=as_of, weighting=agent_weighting,
            outcome_dates_by_ref=outcome_dates_by_ref,
        )
        results.append(replace(res, subject_key=subject_by_agent.get(g), subject_did=subject_did))
    results.sort(key=lambda r: r.cgr_score, reverse=True)
    return results


def _rotation_verifier(pubkey_hex: str, message: bytes, sig_hex: str) -> bool:
    """Ed25519 verify capability for rotation links — lazy `cryptography` import keeps
    the pure scoring path crypto-free. Reuses issuance.make_verifier (the same
    primitive that checks Foundation attestations)."""
    from aml.cgr.issuance import make_verifier
    try:
        return make_verifier(bytes.fromhex(pubkey_hex))(message, sig_hex)
    except Exception:
        return False


def compute_scores(decision_trail, store_manager, tenant_id: str, *,
                   reviews: Iterable[ReviewEvent] | None = None,
                   rotations: Iterable | None = None, as_of: str | None = None,
                   weighting: "WeightingConfig | None" = None,
                   capability_profiles: dict | None = None,
                   limit: int = 500, offset: int = 0) -> list[CGRResult]:
    """Live path: load the tenant's substrate, then score. Reviews + rotation proofs
    are auto-loaded from their stores when the caller doesn't supply them (None);
    pass () to force the empty baseline, or an explicit list. Rotation links are
    verified (Ed25519) before an agent's key-history is folded into one identity.

    v2 (Ticket #13): `weighting` (recency/λ/N_lift/stake) and `capability_profiles`
    (J-Space cap_d per identity) are pass-through seams — both default to the neutral
    v1 behaviour. No profile source is wired into the live path yet (Part C)."""
    rows = load_substrate(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
    if reviews is None:
        reviews = load_reviews(store_manager, tenant_id)
    if rotations is None:
        rotations = load_rotations(store_manager, tenant_id)
    return compute_scores_from_rows(rows, reviews=reviews, rotations=rotations,
                                    verify=_rotation_verifier, as_of=as_of,
                                    weighting=weighting, capability_profiles=capability_profiles)


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
        "agent_handle": r.agent_handle,          # human-readable label (facet@territory)
        "subject_key": r.subject_key,            # CGR #5: current operational GEIANT pubkey (or null)
        "subject_did": r.subject_did,            # CGR #7: identity anchor did:key — stable across rotation
        "dimension": r.dimension,
        "tier": tier,
        "cgr_score": r.cgr_score,
        "confidence": r.confidence,
        "n_resolved": r.n_resolved,
        "capability_tier": r.capability_tier,
        "as_of": r.as_of,
        "rationale": rationale,
    }
