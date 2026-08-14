"""CGR substrate access — the decision↔outcome join, single source of truth.

Owns the read/join over Ticket-#1's captured substrate:
  * decisions live in `decision_records.parameters` (JSONB) — read via an injected
    DecisionTrailService (`query_decisions`);
  * outcomes/reviews/rotations live in append-only GMP stores — read via the
    injected StoreManager's backend `scoped_audit(tenant_id)` (CGR #12), which on
    Postgres runs under the REAL tenant RLS context so the DATABASE enforces
    isolation under the restricted runtime role; a Python `tenant_id ==` + cgr_schema
    check stays as belt-and-suspenders. (Genuine cross-tenant admin still uses
    `audit()`.)

Both the `GET /v1/cgr/substrate/export` route and the scoring engine call
`load_substrate()`, so the join lives in exactly one place. `export_rows()`
reproduces the export's historical 10-key row shape byte-for-byte.

Import isolation: this module imports ONLY stdlib. DecisionTrailService and
StoreManager are passed in as arguments, never imported — keeping the scoring
core free of portal/billing/UI (and even of the cloud service classes).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Store identity + schema marker for CGR outcomes (owned here; re-exported to
# aml.cloud.demo_routes, which keeps the write path).
CGR_OUTCOMES_STORE = "cgr-outcomes"
CGR_OUTCOME_SCHEMA = "cgr.outcome.v1"
CGR_REVIEWS_STORE = "cgr-reviews"
CGR_REVIEW_SCHEMA = "cgr.review.v1"
CGR_ROTATION_STORE = "cgr-identity"   # Ticket #7: append-only key-rotation proofs
from aml.cgr.identity import CGR_ROTATION_SCHEMA, RotationProof  # noqa: E402  (schema tag + dataclass)


@dataclass
class DecisionRow:
    """One governed decision joined to its latest outcome. Fields are exactly the
    export's keys; scoring reads a subset (agent_handle, decision,
    verifiability_tag, invoice_ref, agent_tier, outcome, agent_key)."""
    decision_id: str
    invoice_ref: str | None
    agent_handle: str | None
    agent_tier: float | None
    decision: str | None
    reason_code: str | None
    verifiability_tag: str | None
    created_at: datetime | None
    outcome: str | None
    outcome_date: datetime | None
    # CGR identity binding (Ticket #5): the acting agent's GEIANT Ed25519 pubkey,
    # captured at decision time. None on legacy rows → aggregate by handle, unbound.
    agent_key: str | None = None


@dataclass
class ReviewEvent:
    """A funder/analyst review of one certification. NOT captured by Ticket #1 yet
    (no review-intake path), so live scoring runs with reviews=[]. Defined here as
    the forward-looking schema the engine already consumes; the synthetic
    validation fixture supplies these to exercise the reviewer-weighted signal."""
    invoice_ref: str
    agent_handle: str
    reviewer: str
    rating: float
    # CGR v2 (Ticket #13): review timestamp for recency weighting. Optional/None ⇒
    # recency defaults to 1 (v1). Populated by load_reviews from the record's valid_from.
    review_date: datetime | None = None


# -- outcome-store read helpers (moved verbatim from demo_routes; single owner) --

def _effective_at(m):
    return m.valid_from or m.written_at


def _sort_key(m):
    # Latest wins by valid_from; ref (monotonic insert id) breaks same-instant ties
    # so a same-day correction always beats the record it revised.
    return (_effective_at(m), m.ref or 0)


# ── decrypted-substrate cache (perf hardening, CGR read path) ────────────────
# `scoped_audit` materializes + DECRYPTS every tenant memory. On a large tenant a
# single /v1/cgr/scores read decrypts thousands of rows; under load (many concurrent
# reads, or a background sweep competing for CPU) the decrypt storm made reads slow
# and — worse — return an INCOMPLETE join, so agent scores/n_resolved swung between
# reads. This short-TTL cache bounds the repeated decrypt cost WITHOUT capping the
# scan (every row is still materialized once per TTL window, never dropped). The
# write path (aml.cloud.demo_routes) calls `invalidate_substrate_cache(tenant_id)`
# after every outcome/review write, so a post-then-read (e.g. a live sim run) never
# observes a stale substrate.
#
# DEFAULT OFF (TTL=0): the cache is correct ONLY if every outcome/review write goes
# through the invalidating helpers. That holds for the HTTP write path but NOT for
# tests / fixtures that write directly to the backend — so it ships DISABLED and is
# enabled per-deploy via CGR_SUBSTRATE_CACHE_TTL_S once a deploy confirms all its
# write paths invalidate. Finding 3 (manifold no longer starves HTTP) is what makes
# reads stable by default; this cache is an opt-in cost bound on top.
import os as _os
import threading as _threading
import time as _time

_SUBSTRATE_CACHE_TTL_S = float(_os.environ.get("CGR_SUBSTRATE_CACHE_TTL_S", "0"))
_SUBSTRATE_CACHE: dict[str, tuple[float, list]] = {}
_SUBSTRATE_CACHE_LOCK = _threading.Lock()


def invalidate_substrate_cache(tenant_id: str | None = None) -> None:
    """Drop cached decrypted substrate for a tenant (or all tenants if None). Called
    by the write path so a just-written outcome/review is immediately visible."""
    with _SUBSTRATE_CACHE_LOCK:
        if tenant_id is None:
            _SUBSTRATE_CACHE.clear()
        else:
            _SUBSTRATE_CACHE.pop(tenant_id, None)


def _materialize_scoped_audit(backend, tenant_id: str) -> list:
    fn = getattr(backend, "scoped_audit", None)
    if callable(fn):
        return list(fn(tenant_id))
    return [m for m in backend.audit() if m.tenant_id == tenant_id]


def _scoped_audit(backend, tenant_id: str):
    """Tenant-scoped audit (CGR #12). Prefer the backend's RLS-aware
    `scoped_audit` — Postgres routes it through the REAL tenant RLS context, so the
    DATABASE enforces isolation under the restricted runtime role — and fall back
    to filtering `audit()` for backends that don't implement it (sqlite/reference).
    This replaces the `audit()` (admin/all-tenant) dumps for tenant reads; callers
    keep their own `tenant_id ==` check as belt-and-suspenders. Stdlib-only (getattr).

    Returns a fully-materialized list (never a single-use generator) so callers and
    the TTL cache can iterate it repeatedly. Completeness is preserved — the whole
    tenant scan is materialized; only the *repeat decrypt cost* is bounded."""
    if _SUBSTRATE_CACHE_TTL_S <= 0:
        return _materialize_scoped_audit(backend, tenant_id)
    now = _time.monotonic()
    with _SUBSTRATE_CACHE_LOCK:
        hit = _SUBSTRATE_CACHE.get(tenant_id)
        if hit is not None and (now - hit[0]) < _SUBSTRATE_CACHE_TTL_S:
            return hit[1]
    rows = _materialize_scoped_audit(backend, tenant_id)
    with _SUBSTRATE_CACHE_LOCK:
        _SUBSTRATE_CACHE[tenant_id] = (now, rows)
    return rows


def _tenant_outcomes(backend, tenant_id: str) -> list:
    """Every CGR outcome record for a tenant. Reads through `scoped_audit` (RLS-
    enforced on Postgres under the restricted role) + a Python tenant check as
    belt-and-suspenders. Correctness over performance (POC scale)."""
    rows = []
    for m in _scoped_audit(backend, tenant_id):
        md = m.metadata or {}
        if md.get("cgr_schema") == CGR_OUTCOME_SCHEMA and m.tenant_id == tenant_id:
            rows.append(m)
    return rows


def _latest_for(outcomes: list, invoice_ref: str):
    """The current outcome for an invoice_ref: latest by valid_from. Append-safe —
    correct whether or not the backend marked the prior via supersede()."""
    cands = [m for m in outcomes if (m.metadata or {}).get("subject") == invoice_ref]
    return max(cands, key=_sort_key) if cands else None


# -- review-store read helpers ------------------------------------------------
# Reviews are MANY-per-invoice (many reviewers rate one certification), so the
# dedup/revision key is the (subject, reviewer_handle) PAIR — not the invoice
# alone. _latest_for (one-per-invoice) is deliberately NOT reused here.

def _tenant_reviews(backend, tenant_id: str) -> list:
    """Every CGR review record for a tenant. Mirrors _tenant_outcomes: reads through
    `scoped_audit` (RLS-enforced on Postgres) + the tenant check as belt-and-suspenders."""
    rows = []
    for m in _scoped_audit(backend, tenant_id):
        md = m.metadata or {}
        if md.get("cgr_schema") == CGR_REVIEW_SCHEMA and m.tenant_id == tenant_id:
            rows.append(m)
    return rows


def _latest_review_for(reviews: list, invoice_ref: str, reviewer_handle: str):
    """Current review for ONE (invoice_ref, reviewer_handle) pair — latest by
    valid_from. Used by the write path to dedup/supersede a reviewer's own prior."""
    cands = [m for m in reviews
             if (m.metadata or {}).get("subject") == invoice_ref
             and (m.metadata or {}).get("reviewer_handle") == reviewer_handle]
    return max(cands, key=_sort_key) if cands else None


def _latest_reviews_by_pair(reviews: list) -> list:
    """One record per (subject, reviewer_handle) — the latest by _sort_key. Keeps
    the newest rating per reviewer instead of collapsing to one per invoice."""
    latest: dict = {}
    for m in reviews:
        md = m.metadata or {}
        key = (md.get("subject"), md.get("reviewer_handle"))
        if key[0] is None or key[1] is None:
            continue
        if key not in latest or _sort_key(m) > _sort_key(latest[key]):
            latest[key] = m
    return list(latest.values())


def load_reviews(store_manager, tenant_id: str) -> list[ReviewEvent]:
    """One ReviewEvent per (invoice, reviewer), tenant-scoped, latest rating wins.
    Feeds the reviewer-calibration bridge in engine.compute_scores. Deps injected;
    stdlib-only — import isolation preserved."""
    backend = store_manager.get_or_create_named(CGR_REVIEWS_STORE).backend
    events = []
    for m in _latest_reviews_by_pair(_tenant_reviews(backend, tenant_id)):
        md = m.metadata or {}
        obj = md.get("object")
        events.append(ReviewEvent(
            invoice_ref=md.get("subject"),
            agent_handle=md.get("agent_handle"),
            reviewer=md.get("reviewer_handle"),
            rating=float(obj) if obj is not None else 0.0,
            review_date=_effective_at(m),        # CGR v2: recency timestamp
        ))
    return events


def _tenant_rotations(backend, tenant_id: str) -> list:
    """Every CGR rotation-proof record for a tenant (mirrors _tenant_reviews):
    reads through `scoped_audit` (RLS-enforced on Postgres) + the tenant check."""
    rows = []
    for m in _scoped_audit(backend, tenant_id):
        md = m.metadata or {}
        if md.get("cgr_schema") == CGR_ROTATION_SCHEMA and m.tenant_id == tenant_id:
            rows.append(m)
    return rows


def load_rotations(store_manager, tenant_id: str) -> list[RotationProof]:
    """All captured key-rotation proofs for a tenant, tenant-scoped. RAW — the
    signature is verified at aggregation time (engine), never at read time, so a
    tampered row cannot grant continuity. Deps injected; stdlib-only."""
    backend = store_manager.get_or_create_named(CGR_ROTATION_STORE).backend
    proofs = []
    for m in _tenant_rotations(backend, tenant_id):
        md = m.metadata or {}
        try:
            proofs.append(RotationProof(
                prev_key=md.get("prev_key"),
                new_key=md.get("subject"),          # subject == new_key (the Fact key)
                seq=int(md.get("seq")) if md.get("seq") is not None else 0,
                not_before=md.get("not_before") or "",
                sig=md.get("sig"),
            ))
        except (TypeError, ValueError):
            continue                                # malformed row → skip (verified later anyway)
    return proofs


def export_rotations(store_manager, tenant_id: str) -> list[dict]:
    """Serialize the captured rotation proofs for read-only exposure (Ticket #10a).

    Each proof is EXACTLY the signed link body + `sig`: {prev_key, new_key, seq,
    not_before, sig}. The values are served RAW as stored (never re-parsed/
    normalized), so JCS-canonicalizing {prev_key,new_key,seq,not_before} reproduces
    the bytes `prev_key` signed — the same object `identity.verify_link` reconstructs.
    The reader (geiant #10b) re-verifies each signature; the server is untrusted
    transport. No verify-and-discard here — self-certifying facts, served raw."""
    backend = store_manager.get_or_create_named(CGR_ROTATION_STORE).backend
    out = []
    for m in _tenant_rotations(backend, tenant_id):
        md = m.metadata or {}
        out.append({
            "prev_key": md.get("prev_key"),
            "new_key": md.get("subject"),           # subject == new_key (the Fact key)
            "seq": md.get("seq"),                   # int as signed — do NOT stringify (byte-parity)
            "not_before": md.get("not_before"),     # raw as signed
            "sig": md.get("sig"),
        })
    return out


def export_reviews(store_manager, tenant_id: str) -> list[dict]:
    """Latest review per (invoice, reviewer), serialized for the additive reviews[]
    on /substrate/export."""
    backend = store_manager.get_or_create_named(CGR_REVIEWS_STORE).backend
    out = []
    for m in _latest_reviews_by_pair(_tenant_reviews(backend, tenant_id)):
        md = m.metadata or {}
        obj = md.get("object")
        out.append({
            "invoice_ref": md.get("subject"),
            "reviewer_handle": md.get("reviewer_handle"),
            "agent_handle": md.get("agent_handle"),
            "rating": float(obj) if obj is not None else None,
            "review_date": _effective_at(m).isoformat() if _effective_at(m) else None,
        })
    return out


def load_substrate(decision_trail, store_manager, tenant_id: str, *,
                   limit: int = 500, offset: int = 0) -> list[DecisionRow]:
    """Join this tenant's governed decisions to their latest outcomes.

    Same logic the export route used inline: build a latest-outcome-per-invoice
    index (by valid_from, ref tiebreak), then left-join each decision on
    invoice_ref (tag-agnostic — any decision, resolved or not). Returns DecisionRow
    objects; the join happens here and nowhere else.
    """
    backend = store_manager.get_or_create_named(CGR_OUTCOMES_STORE).backend
    decisions = decision_trail.query_decisions(tenant_id=tenant_id, limit=limit, offset=offset)

    by_ref: dict = {}
    for m in _tenant_outcomes(backend, tenant_id):
        ref = (m.metadata or {}).get("subject")
        if ref is None:
            continue
        if ref not in by_ref or _sort_key(m) > _sort_key(by_ref[ref]):
            by_ref[ref] = m

    rows: list[DecisionRow] = []
    for rec in decisions:
        p = rec.parameters or {}
        inv_ref = p.get("invoice_ref", p.get("invoice_id"))
        om = by_ref.get(inv_ref)
        rows.append(DecisionRow(
            decision_id=rec.decision_id,
            invoice_ref=inv_ref,
            agent_handle=p.get("agent_handle"),
            agent_tier=p.get("agent_tier"),
            decision=p.get("decision"),
            reason_code=p.get("reason_code"),
            verifiability_tag=p.get("verifiability_tag"),
            created_at=rec.created_at,
            outcome=(om.metadata or {}).get("object") if om else None,
            outcome_date=_effective_at(om) if om else None,
            agent_key=p.get("agent_key"),            # CGR #5: captured at decision time, never back-resolved
        ))
    return rows


def export_rows(rows: list[DecisionRow]) -> list[dict]:
    """Serialize DecisionRows to the export's JSON shape, byte-for-byte (datetimes
    → isoformat or None). Guarded by a regression test. The first 10 keys are the
    historical contract, in order; `agent_key` (Ticket #5) is appended as the 11th."""
    return [{
        "decision_id": r.decision_id,
        "invoice_ref": r.invoice_ref,
        "agent_handle": r.agent_handle,
        "agent_tier": r.agent_tier,
        "decision": r.decision,
        "reason_code": r.reason_code,
        "verifiability_tag": r.verifiability_tag,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "outcome": r.outcome,
        "outcome_date": r.outcome_date.isoformat() if r.outcome_date else None,
        "agent_key": r.agent_key,                    # 11th key (appended, Ticket #5)
    } for r in rows]
