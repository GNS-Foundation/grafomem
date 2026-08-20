"""Cloud Metering Phase 1 — governed-decision usage read-model (ADDITIVE).

``GET /v1/usage/current`` returns the CURRENT period's governed-decision count,
derived live from ``decision_records`` via ``DecisionTrailService.get_usage`` (see
that method for the predicate and why replays are not counted), plus a certify/reject
breakdown, the tenant's plan, and a PLACEHOLDER included allotment.

Hard scope of Phase 1: read-model ONLY. No Stripe, no money, no enforcement. The
``included_allotment`` values are PLACEHOLDERS, not final pricing, and nothing is
capped by them. Auth is tenant-scoped; isolation is the tenant context + the
``tenant_id = %s`` filter inside ``get_usage``.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from aml.cloud.plan_config import (
    INCLUDED_ALLOTMENT,
    INCLUDED_ALLOTMENT_IS_PLACEHOLDER,
    WARN_PCT,
)
from aml.server.scopes import require_scope

# Back-compat alias for the Phase-1 name; the canonical tunables now live in plan_config
# (INCLUDED_ALLOTMENT + WARN_PCT). Still display-only, still not enforced.
PLACEHOLDER_INCLUDED_ALLOTMENT = INCLUDED_ALLOTMENT


def _minus_one_month(dt: datetime) -> datetime:
    """dt shifted back one calendar month, clamping the day to the target month."""
    y, m = dt.year, dt.month - 1
    if m == 0:
        y, m = y - 1, 12
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)


def resolve_current_period(
    tenant_id: str,
    subscription_period_start: datetime | None = None,
    subscription_period_end: datetime | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime, str]:
    """Return ``(period_start, period_end, source)`` for the tenant's current usage
    window, half-open ``[start, end)`` to match ``DecisionTrailService.get_usage``.

    * subscription — when BOTH real Stripe ``current_period_start`` and ``…_end`` are
      known, use them verbatim: ``[start, end)``, source ``"subscription"``. This is the
      Phase-3d fix: the true billing window (correct for annual/any interval), not an
      assumed monthly span.
    * subscription (end only) — legacy/back-compat when only the end is known (start not
      yet populated from webhooks): the monthly window ``[end - 1 month, end)``, source
      ``"subscription_end_only"``.
    * else — the UTC calendar month containing ``now()``, source ``"calendar_month"``
      (free/no-sub tenants).
    """
    now = now or datetime.now(tz=timezone.utc)
    if subscription_period_start is not None and subscription_period_end is not None:
        start = subscription_period_start
        end = subscription_period_end
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return start, end, "subscription"
    if subscription_period_end is not None:
        end = subscription_period_end
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return _minus_one_month(end), end, "subscription_end_only"

    now = now.astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end, "calendar_month"


def _compute_state(
    governed_decisions: int,
    included_allotment: int | None,
    warn_pct: float = WARN_PCT,
) -> tuple[str, float | None]:
    """Classify a period's governed-decision count → ``(state, pct_used)``.

    ``state ∈ {"normal", "approaching", "at_or_over"}``. ``included_allotment`` of
    ``None`` (or 0) — enterprise/custom, or no allotment configured — yields
    ``("normal", None)``: nothing to be near, and no divide-by-None.

    DISPLAY/UX ONLY. This CLASSIFIES usage for the console; it computes no limit and
    NEVER blocks, denies, or throttles a decision. ``at_or_over`` is informational.
    """
    if not included_allotment:  # None or 0
        return "normal", None
    pct_used = round(100.0 * governed_decisions / included_allotment, 1)
    if governed_decisions >= included_allotment:
        return "at_or_over", pct_used
    if governed_decisions >= warn_pct * included_allotment:
        return "approaching", pct_used
    return "normal", pct_used


def resolve_usage_state(
    tenant_id: str,
    decision_trail,
    tenant_manager=None,
    stripe_billing=None,
) -> dict:
    """Full current-period usage-state read-model for a tenant (Metering Phase 2).

    Reuses the Phase-1 ``DecisionTrailService.get_usage`` (UNMODIFIED) and adds the
    display-only ``state`` / ``pct_used`` classification. No enforcement, no ceiling —
    a tenant ``at_or_over`` its (placeholder) allotment is never blocked.
    """
    plan = "starter"
    if tenant_manager is not None:
        try:
            info = tenant_manager.get_tenant(tenant_id)
            if info is not None and info.plan:
                plan = info.plan
        except Exception:
            pass

    sub_start = sub_end = None
    if stripe_billing is not None:
        try:
            sub = stripe_billing.get_subscription(tenant_id)
            if sub:
                sub_start = getattr(sub, "current_period_start", None)
                sub_end = sub.current_period_end
        except Exception:
            sub_start = sub_end = None

    start, end, source = resolve_current_period(tenant_id, sub_start, sub_end)
    usage = decision_trail.get_usage(tenant_id, start, end)
    allotment = INCLUDED_ALLOTMENT.get(plan)
    state, pct_used = _compute_state(usage["governed_decisions"], allotment)

    return {
        "plan": plan,
        "period": {"start": start.isoformat(), "end": end.isoformat(), "source": source},
        "governed_decisions": usage["governed_decisions"],
        "breakdown": usage["breakdown"],
        "included_allotment": allotment,
        "is_placeholder": INCLUDED_ALLOTMENT_IS_PLACEHOLDER,
        "pct_used": pct_used,
        "state": state,
    }


def create_usage_router() -> APIRouter:
    """Mount ``/v1/usage``. Services are read LAZILY from ``request.app.state`` so this
    is safe to include before ``tenant_manager`` / ``stripe_billing`` are constructed."""
    router = APIRouter(prefix="/v1/usage", tags=["Usage"])

    @router.get("/current")
    async def current_usage(request: Request):
        ctx = getattr(request.state, "tenant", None)
        if ctx is None:
            raise HTTPException(401, "Authentication required")
        tenant_id = ctx.tenant_id
        require_scope(request, "decisions:read")

        decision_trail = getattr(request.app.state, "decision_trail", None)
        if decision_trail is None:
            raise HTTPException(503, "Decision trail not available")
        tm = getattr(request.app.state, "tenant_manager", None)
        sb = getattr(request.app.state, "stripe_billing", None)

        s = resolve_usage_state(tenant_id, decision_trail, tenant_manager=tm, stripe_billing=sb)
        # Additive: Phase-1 shape preserved (incl. the old key) + state/pct_used/is_placeholder.
        return {**s, "included_allotment_is_placeholder": s["is_placeholder"]}

    return router
