"""
GRAFOMEM cloud management routes — tenant, usage, and compliance endpoints.

Mounted at ``/v1/cloud`` on the main FastAPI app.  All endpoints use Pydantic
response models and follow the same patterns as the GMP endpoints in
``aml.server.app``.

These routes are operator-facing (admin API) — tenant self-service, billing
dashboards, and compliance reporting.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("grafomem.cloud.routes")


# ============================================================================
# Pydantic models — request / response
# ============================================================================

class TenantLimitsResponse(BaseModel):
    """Plan-level resource ceilings."""
    max_memories: int
    max_stores: int
    max_requests_per_minute: int


class TenantResponse(BaseModel):
    """Full tenant representation."""
    id: str
    name: str
    api_key: str
    plan: str
    created_at: datetime
    limits: TenantLimitsResponse


class CreateTenantRequest(BaseModel):
    """Request body for tenant provisioning."""
    name: str
    plan: str = "starter"


class RotateKeyResponse(BaseModel):
    """Returned after a successful key rotation."""
    tenant_id: str
    new_api_key: str


class UsageSummaryResponse(BaseModel):
    """Aggregated usage for a billing period."""
    tenant_id: str
    period: str
    writes: int = 0
    reads: int = 0
    deletes: int = 0
    supersedes: int = 0
    total_bytes: int = 0
    total_operations: int = 0


class AuditRecordResponse(BaseModel):
    """A single conformance audit result."""
    id: str
    tenant_id: str
    store_id: str
    conformance_rate: float
    capabilities: list[str] = Field(default_factory=list)
    audited_at: datetime
    report_json: str | None = None


class ComplianceDashboardResponse(BaseModel):
    """Global compliance status across all tenants."""
    tenants: list[AuditRecordResponse] = Field(default_factory=list)


# ============================================================================
# Helpers
# ============================================================================

def _tenant_manager(request: Request):
    """Extract the TenantManager from app state."""
    mgr = getattr(request.app.state, "tenant_manager", None)
    if mgr is None:
        raise HTTPException(
            503, "Cloud layer not configured — tenant_manager not available",
        )
    return mgr


def _metering(request: Request):
    """Extract the MeteringService from app state."""
    svc = getattr(request.app.state, "metering_service", None)
    if svc is None:
        raise HTTPException(
            503, "Cloud layer not configured — metering_service not available",
        )
    return svc


def _compliance(request: Request):
    """Extract the ComplianceTracker from app state."""
    tracker = getattr(request.app.state, "compliance_tracker", None)
    if tracker is None:
        raise HTTPException(
            503, "Cloud layer not configured — compliance_tracker not available",
        )
    return tracker


def _tenant_to_response(info) -> TenantResponse:
    """Convert a TenantInfo dataclass to a Pydantic response model."""
    return TenantResponse(
        id=info.id,
        name=info.name,
        api_key=info.api_key,
        plan=info.plan,
        created_at=info.created_at,
        limits=TenantLimitsResponse(
            max_memories=info.limits.max_memories,
            max_stores=info.limits.max_stores,
            max_requests_per_minute=info.limits.max_requests_per_minute,
        ),
    )


def _audit_to_response(record) -> AuditRecordResponse:
    """Convert an AuditRecord dataclass to a Pydantic response model."""
    return AuditRecordResponse(
        id=record.id,
        tenant_id=record.tenant_id,
        store_id=record.store_id,
        conformance_rate=record.conformance_rate,
        capabilities=record.capabilities,
        audited_at=record.audited_at,
        report_json=record.report_json,
    )


# ============================================================================
# Router
# ============================================================================

router = APIRouter(prefix="/v1/cloud", tags=["Cloud Management"])


@router.post("/tenants", response_model=TenantResponse, status_code=201)
async def create_tenant(req: CreateTenantRequest, request: Request):
    """Provision a new tenant with the specified plan."""
    mgr = _tenant_manager(request)
    try:
        info = mgr.create_tenant(name=req.name, plan=req.plan)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _tenant_to_response(info)


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(request: Request):
    """List all provisioned tenants."""
    mgr = _tenant_manager(request)
    tenants = mgr.list_tenants()
    return [_tenant_to_response(t) for t in tenants]


@router.get("/tenants/{tenant_id}", response_model=TenantResponse)
async def get_tenant(tenant_id: str, request: Request):
    """Retrieve a single tenant by ID."""
    mgr = _tenant_manager(request)
    info = mgr.get_tenant(tenant_id)
    if info is None:
        raise HTTPException(404, f"Tenant {tenant_id!r} not found")
    return _tenant_to_response(info)


@router.post(
    "/tenants/{tenant_id}/rotate-key", response_model=RotateKeyResponse,
)
async def rotate_key(tenant_id: str, request: Request):
    """Revoke the current API key and issue a new one."""
    mgr = _tenant_manager(request)
    try:
        new_key = mgr.revoke_key(tenant_id)
    except KeyError:
        raise HTTPException(404, f"Tenant {tenant_id!r} not found")
    return RotateKeyResponse(tenant_id=tenant_id, new_api_key=new_key)


@router.get(
    "/tenants/{tenant_id}/usage", response_model=UsageSummaryResponse,
)
async def get_usage(
    tenant_id: str, request: Request, period: str = "current_month",
):
    """Retrieve aggregated usage for a tenant's billing period."""
    svc = _metering(request)

    # Verify tenant exists
    mgr = _tenant_manager(request)
    if mgr.get_tenant(tenant_id) is None:
        raise HTTPException(404, f"Tenant {tenant_id!r} not found")

    try:
        summary = svc.get_usage(tenant_id, period=period)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return UsageSummaryResponse(
        tenant_id=summary.tenant_id,
        period=summary.period,
        writes=summary.writes,
        reads=summary.reads,
        deletes=summary.deletes,
        supersedes=summary.supersedes,
        total_bytes=summary.total_bytes,
        total_operations=summary.total_operations,
    )


@router.get(
    "/tenants/{tenant_id}/compliance",
    response_model=list[AuditRecordResponse],
)
async def get_compliance(
    tenant_id: str, request: Request, limit: int = 10,
):
    """Retrieve conformance audit history for a tenant."""
    tracker = _compliance(request)

    # Verify tenant exists
    mgr = _tenant_manager(request)
    if mgr.get_tenant(tenant_id) is None:
        raise HTTPException(404, f"Tenant {tenant_id!r} not found")

    records = tracker.get_history(tenant_id, limit=limit)
    return [_audit_to_response(r) for r in records]


@router.get(
    "/compliance/status", response_model=ComplianceDashboardResponse,
)
async def compliance_dashboard(request: Request):
    """Global compliance dashboard — latest audit per tenant."""
    tracker = _compliance(request)
    records = tracker.get_all_latest()
    return ComplianceDashboardResponse(
        tenants=[_audit_to_response(r) for r in records],
    )


# ============================================================================
# Billing endpoints
# ============================================================================

class CheckoutRequest(BaseModel):
    """Request body for creating a Stripe Checkout session."""
    tenant_id: str
    plan: str = "pro"
    success_url: str = "https://cloud.grafomem.com/portal.html?upgraded=true"
    cancel_url: str = "https://cloud.grafomem.com/portal.html"


def _stripe_billing(request: Request):
    """Extract StripeBillingService from app state."""
    svc = getattr(request.app.state, "stripe_billing", None)
    if svc is None:
        raise HTTPException(503, "Stripe billing not configured")
    return svc


@router.post("/billing/checkout")
async def billing_checkout(req: CheckoutRequest, request: Request):
    """Create a Stripe Checkout Session and return the redirect URL."""
    svc = _stripe_billing(request)
    try:
        url = svc.create_checkout_session(
            tenant_id=req.tenant_id, plan=req.plan,
            success_url=req.success_url, cancel_url=req.cancel_url,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"checkout_url": url}


@router.post("/billing/webhook")
async def billing_webhook(request: Request):
    """Stripe webhook receiver.

    This endpoint is excluded from Bearer token auth — Stripe sends its own
    signature in the ``Stripe-Signature`` header.
    """
    svc = _stripe_billing(request)
    payload = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        result = svc.handle_webhook(payload, sig)
    except Exception as exc:
        logger.error("Webhook processing failed: %s", exc)
        raise HTTPException(400, f"Webhook error: {exc}")
    return result


class SubscriptionResponse(BaseModel):
    """Current subscription status."""
    tenant_id: str
    plan: str
    status: str
    stripe_subscription_id: str | None = None
    current_period_end: str | None = None


@router.get("/billing/subscription/{tenant_id}", response_model=SubscriptionResponse)
async def get_subscription(tenant_id: str, request: Request):
    """Retrieve a tenant's current Stripe subscription."""
    svc = _stripe_billing(request)
    sub = svc.get_subscription(tenant_id)
    if sub is None:
        return SubscriptionResponse(tenant_id=tenant_id, plan="starter", status="none")
    return SubscriptionResponse(
        tenant_id=sub.tenant_id,
        plan=sub.plan,
        status=sub.status,
        stripe_subscription_id=sub.stripe_subscription_id,
        current_period_end=sub.current_period_end.isoformat() if sub.current_period_end else None,
    )


@router.post("/billing/cancel/{tenant_id}")
async def cancel_subscription(tenant_id: str, request: Request):
    """Cancel a tenant's Stripe subscription."""
    svc = _stripe_billing(request)
    ok = svc.cancel_subscription(tenant_id)
    if not ok:
        raise HTTPException(404, "No active subscription found")
    return {"status": "canceled", "tenant_id": tenant_id}


# ============================================================================
# Compliance badge endpoints
# ============================================================================

_BADGE_SVG_TEMPLATE = """\
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" role="img" aria-label="{label}">
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="{width}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{left_width}" height="20" fill="#555"/>
    <rect x="{left_width}" width="{right_width}" height="20" fill="{color}"/>
    <rect width="{width}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" text-rendering="geometricPrecision" font-size="11">
    <text x="{left_center}" y="15" fill="#010101" fill-opacity=".3">{left_text}</text>
    <text x="{left_center}" y="14">{left_text}</text>
    <text x="{right_center}" y="15" fill="#010101" fill-opacity=".3">{right_text}</text>
    <text x="{right_center}" y="14">{right_text}</text>
  </g>
</svg>"""


from starlette.responses import Response as StarletteResponse


@router.get("/compliance/badge/{tenant_id}.svg")
async def compliance_badge_svg(tenant_id: str, request: Request):
    """Return an embeddable SVG compliance badge (shields.io style)."""
    tracker = _compliance(request)
    latest = tracker.get_latest(tenant_id)

    left_text = "GMP v0.2"
    left_width = 62

    if latest is None:
        right_text = "not audited"
        color = "#9e9e9e"  # gray
    elif latest.conformance_rate >= 0.95:
        right_text = f"M8: {latest.conformance_rate:.3f} | PASS"
        color = "#4c1"  # green
    else:
        right_text = f"M8: {latest.conformance_rate:.3f} | FAIL"
        color = "#e05d44"  # red

    right_width = len(right_text) * 7 + 10
    width = left_width + right_width

    svg = _BADGE_SVG_TEMPLATE.format(
        width=width,
        left_width=left_width,
        right_width=right_width,
        left_center=left_width // 2,
        right_center=left_width + right_width // 2,
        left_text=left_text,
        right_text=right_text,
        color=color,
        label=f"{left_text}: {right_text}",
    )

    return StarletteResponse(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache, max-age=300"},
    )


@router.get("/compliance/badge/{tenant_id}.json")
async def compliance_badge_json(tenant_id: str, request: Request):
    """Shields.io endpoint schema for dynamic badge generation."""
    tracker = _compliance(request)
    latest = tracker.get_latest(tenant_id)

    if latest is None:
        return {
            "schemaVersion": 1,
            "label": "GMP v0.2",
            "message": "not audited",
            "color": "lightgrey",
        }

    passed = latest.conformance_rate >= 0.95
    return {
        "schemaVersion": 1,
        "label": "GMP v0.2",
        "message": f"M8: {latest.conformance_rate:.3f} | {'PASS' if passed else 'FAIL'}",
        "color": "brightgreen" if passed else "red",
    }
