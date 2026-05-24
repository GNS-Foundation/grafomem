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
