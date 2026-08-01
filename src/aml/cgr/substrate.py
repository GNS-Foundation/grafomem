"""CGR substrate access — the decision↔outcome join, single source of truth.

Owns the read/join over Ticket-#1's captured substrate:
  * decisions live in `decision_records.parameters` (JSONB) — read via an injected
    DecisionTrailService (`query_decisions`);
  * outcomes live in the append-only `cgr-outcomes` GMP store — read via an
    injected StoreManager's backend `audit()`, filtered to the tenant + the
    cgr_schema marker (audit() is admin/all-tenant, so the tenant predicate here
    IS the isolation boundary — same guarantee as the export route).

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


@dataclass
class DecisionRow:
    """One governed decision joined to its latest outcome. Fields are exactly the
    export's 10 keys; scoring reads a subset (agent_handle, decision,
    verifiability_tag, invoice_ref, agent_tier, outcome)."""
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


# -- outcome-store read helpers (moved verbatim from demo_routes; single owner) --

def _effective_at(m):
    return m.valid_from or m.written_at


def _sort_key(m):
    # Latest wins by valid_from; ref (monotonic insert id) breaks same-instant ties
    # so a same-day correction always beats the record it revised.
    return (_effective_at(m), m.ref or 0)


def _tenant_outcomes(backend, tenant_id: str) -> list:
    """Every CGR outcome record for a tenant. `audit()` is an admin (all-tenant)
    dump over the shared store, so we filter by tenant_id + the cgr_schema marker.
    Correctness over performance (POC scale); paginate/optimize later."""
    rows = []
    for m in backend.audit():
        md = m.metadata or {}
        if md.get("cgr_schema") == CGR_OUTCOME_SCHEMA and m.tenant_id == tenant_id:
            rows.append(m)
    return rows


def _latest_for(outcomes: list, invoice_ref: str):
    """The current outcome for an invoice_ref: latest by valid_from. Append-safe —
    correct whether or not the backend marked the prior via supersede()."""
    cands = [m for m in outcomes if (m.metadata or {}).get("subject") == invoice_ref]
    return max(cands, key=_sort_key) if cands else None


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
        ))
    return rows


def export_rows(rows: list[DecisionRow]) -> list[dict]:
    """Serialize DecisionRows to the export's historical 10-key JSON shape,
    byte-for-byte (datetimes → isoformat or None). Guarded by a regression test."""
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
    } for r in rows]
