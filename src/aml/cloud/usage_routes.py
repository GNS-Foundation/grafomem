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

from aml.server.scopes import require_scope

# PLACEHOLDER allotments — display anchors for the usage bar ONLY. NOT final pricing,
# NOT enforced. Real allotments/pricing are a later, separately-attested step.
PLACEHOLDER_INCLUDED_ALLOTMENT: dict[str, int | None] = {
    "starter": 10_000,
    "pro": 100_000,
    "enterprise": None,  # unlimited / custom — no numeric allotment
}


def _minus_one_month(dt: datetime) -> datetime:
    """dt shifted back one calendar month, clamping the day to the target month."""
    y, m = dt.year, dt.month - 1
    if m == 0:
        y, m = y - 1, 12
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)


def resolve_current_period(
    tenant_id: str,
    subscription_period_end: datetime | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime, str]:
    """Return ``(period_start, period_end, source)`` for the tenant's current usage
    window, half-open ``[start, end)`` to match ``DecisionTrailService.get_usage``.

    * subscription anchor — when ``subscription_period_end`` is known, the monthly
      window ending at it: ``[end - 1 month, end)``, source ``"subscription"``.
    * else — the UTC calendar month containing ``now()``, source ``"calendar_month"``.
    """
    now = now or datetime.now(tz=timezone.utc)
    if subscription_period_end is not None:
        end = subscription_period_end
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return _minus_one_month(end), end, "subscription"

    now = now.astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end, "calendar_month"


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

        # plan — source of truth is tenants.plan (via TenantManager)
        plan = "starter"
        tm = getattr(request.app.state, "tenant_manager", None)
        if tm is not None:
            try:
                info = tm.get_tenant(tenant_id)
                if info is not None and info.plan:
                    plan = info.plan
            except Exception:
                pass

        # optional subscription anchor for the billing period
        sub_end = None
        sb = getattr(request.app.state, "stripe_billing", None)
        if sb is not None:
            try:
                sub = sb.get_subscription(tenant_id)
                sub_end = sub.current_period_end if sub else None
            except Exception:
                sub_end = None

        start, end, source = resolve_current_period(tenant_id, sub_end)
        usage = decision_trail.get_usage(tenant_id, start, end)

        return {
            "period": {"start": start.isoformat(), "end": end.isoformat(), "source": source},
            "governed_decisions": usage["governed_decisions"],
            "breakdown": usage["breakdown"],
            "plan": plan,
            "included_allotment": PLACEHOLDER_INCLUDED_ALLOTMENT.get(plan),
            "included_allotment_is_placeholder": True,
        }

    return router
