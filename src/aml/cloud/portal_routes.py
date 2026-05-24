"""
GRAFOMEM portal routes — self-service tenant portal API.

Mounted at ``/v1/portal`` on the main FastAPI app.  Provides signup, login,
dashboard data, key rotation, and upgrade endpoints.  Authentication uses
JWT tokens issued by :class:`PortalAuth` — stored in the client's
``localStorage`` and sent as ``Authorization: Bearer <jwt>``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("grafomem.cloud.portal")


# ============================================================================
# Pydantic models — request / response
# ============================================================================

class SignupRequest(BaseModel):
    """Portal signup payload."""
    name: str
    email: str
    password: str
    plan: str = "starter"


class LoginRequest(BaseModel):
    """Portal login payload."""
    email: str
    password: str


class TokenResponse(BaseModel):
    """Returned after successful signup or login."""
    token: str
    tenant_id: str
    name: str
    email: str
    api_key: str
    plan: str


class UsageSummaryOut(BaseModel):
    """Usage stats for the dashboard."""
    writes: int = 0
    reads: int = 0
    deletes: int = 0
    supersedes: int = 0
    total_bytes: int = 0
    total_operations: int = 0


class ComplianceOut(BaseModel):
    """Latest conformance snapshot."""
    conformance_rate: float | None = None
    capabilities: list[str] = Field(default_factory=list)
    audited_at: datetime | None = None


class BillingOut(BaseModel):
    """Current billing / subscription status."""
    plan: str = "starter"
    status: str = "active"
    current_period_end: datetime | None = None


class DashboardResponse(BaseModel):
    """Aggregated dashboard data returned by ``GET /v1/portal/me``."""
    tenant_id: str
    name: str
    email: str
    api_key: str
    plan: str
    usage: UsageSummaryOut
    compliance: ComplianceOut | None = None
    billing: BillingOut | None = None


class UpgradeRequest(BaseModel):
    """Plan upgrade payload."""
    plan: str = "pro"


# ============================================================================
# Helpers
# ============================================================================

def _portal_auth(request: Request):
    """Extract the PortalAuth from app state."""
    pa = getattr(request.app.state, "portal_auth", None)
    if pa is None:
        raise HTTPException(503, "Portal auth not configured")
    return pa


def _require_portal_auth(request: Request) -> dict:
    """Verify JWT from Authorization header and return tenant info dict.

    Returns
    -------
    dict
        Keys: ``tenant_id``, ``name``, ``email``, ``api_key``, ``plan``.

    Raises
    ------
    HTTPException
        401 if missing/invalid token.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization: Bearer <token> header")

    token = auth_header[7:].strip()
    pa = _portal_auth(request)
    info = pa.verify_token(token)
    if info is None:
        raise HTTPException(401, "Invalid or expired token")
    return info


def _tenant_manager(request: Request):
    mgr = getattr(request.app.state, "tenant_manager", None)
    if mgr is None:
        raise HTTPException(503, "Tenant manager not configured")
    return mgr


def _metering(request: Request):
    return getattr(request.app.state, "metering_service", None)


def _compliance(request: Request):
    return getattr(request.app.state, "compliance_tracker", None)


def _stripe_billing(request: Request):
    return getattr(request.app.state, "stripe_billing", None)


# ============================================================================
# Router
# ============================================================================

router = APIRouter(prefix="/v1/portal", tags=["Portal"])


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(req: SignupRequest, request: Request):
    """Create a new tenant account with email/password credentials."""
    pa = _portal_auth(request)
    try:
        info, token = pa.signup(
            name=req.name, email=req.email,
            password=req.password, plan=req.plan,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))

    return TokenResponse(token=token, **info)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request):
    """Authenticate with email and password, receive JWT."""
    pa = _portal_auth(request)
    try:
        result = pa.login(email=req.email, password=req.password)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))

    if result is None:
        raise HTTPException(401, "Invalid email or password")

    info, token = result
    return TokenResponse(token=token, **info)


@router.get("/me", response_model=DashboardResponse)
async def get_dashboard(request: Request):
    """Fetch aggregated dashboard data for the authenticated tenant."""
    tenant = _require_portal_auth(request)
    tenant_id = tenant["tenant_id"]

    # Usage
    usage = UsageSummaryOut()
    ms = _metering(request)
    if ms is not None:
        try:
            u = ms.get_usage(tenant_id)
            usage = UsageSummaryOut(
                writes=u.writes, reads=u.reads,
                deletes=u.deletes, supersedes=u.supersedes,
                total_bytes=u.total_bytes,
                total_operations=u.total_operations,
            )
        except Exception:
            pass

    # Compliance
    compliance = None
    ct = _compliance(request)
    if ct is not None:
        try:
            latest = ct.get_latest(tenant_id)
            if latest:
                compliance = ComplianceOut(
                    conformance_rate=latest.conformance_rate,
                    capabilities=latest.capabilities,
                    audited_at=latest.audited_at,
                )
        except Exception:
            pass

    # Billing
    billing = BillingOut(plan=tenant["plan"])
    sb = _stripe_billing(request)
    if sb is not None:
        try:
            sub = sb.get_subscription(tenant_id)
            if sub:
                billing = BillingOut(
                    plan=sub.plan,
                    status=sub.status,
                    current_period_end=sub.current_period_end,
                )
        except Exception:
            pass

    return DashboardResponse(
        tenant_id=tenant_id,
        name=tenant["name"],
        email=tenant["email"],
        api_key=tenant["api_key"],
        plan=tenant["plan"],
        usage=usage,
        compliance=compliance,
        billing=billing,
    )


@router.post("/rotate-key")
async def rotate_key(request: Request):
    """Revoke the current API key and issue a new one."""
    tenant = _require_portal_auth(request)
    mgr = _tenant_manager(request)
    try:
        new_key = mgr.revoke_key(tenant["tenant_id"])
    except KeyError:
        raise HTTPException(404, "Tenant not found")
    return {"api_key": new_key}


@router.post("/upgrade")
async def upgrade(req: UpgradeRequest, request: Request):
    """Initiate Stripe Checkout for a plan upgrade."""
    tenant = _require_portal_auth(request)
    sb = _stripe_billing(request)

    if sb is None:
        raise HTTPException(503, "Billing not configured")

    try:
        url = sb.create_checkout_session(
            tenant_id=tenant["tenant_id"],
            plan=req.plan,
            success_url="https://grafomem-production.up.railway.app/portal?upgraded=true",
            cancel_url="https://grafomem-production.up.railway.app/portal",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return {"checkout_url": url}
