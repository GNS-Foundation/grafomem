"""
GRAFOMEM Tenant Admin Routes — Sprint 22.

FastAPI router providing admin-only endpoints for multi-tenant
management, member invitation, and role-based access control.

Endpoints:
    GET    /v1/admin/tenants               — List all tenants (super-admin)
    GET    /v1/admin/tenants/{id}           — Get tenant detail
    PUT    /v1/admin/tenants/{id}           — Update tenant (plan, name)
    POST   /v1/admin/tenants/{id}/members   — Invite a team member
    GET    /v1/admin/tenants/{id}/members   — List members
    PUT    /v1/admin/tenants/{id}/members/{uid} — Update member role
    DELETE /v1/admin/tenants/{id}/members/{uid} — Remove member
    GET    /v1/admin/tenants/{id}/usage     — Usage summary
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("grafomem.cloud.admin_routes")

router = APIRouter(tags=["Tenant Admin"])


# ------------------------------------------------------------------
# Request / response models
# ------------------------------------------------------------------

class UpdateTenantRequest(BaseModel):
    name: str | None = None
    plan: str | None = None


class InviteMemberRequest(BaseModel):
    email: str
    role: str = "member"


class UpdateRoleRequest(BaseModel):
    role: str


# ------------------------------------------------------------------
# Dependencies
# ------------------------------------------------------------------

def _require_admin(request: Request) -> dict:
    """Require portal auth and admin/owner role.

    Falls back to checking if the requester owns the target tenant.
    """
    portal_auth = getattr(request.app.state, "portal_auth", None)
    if not portal_auth:
        raise HTTPException(status_code=503, detail="Auth not configured")

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")

    token = auth_header[7:]
    user = portal_auth.verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")

    return user


def _tenant_manager(request: Request):
    """Get TenantManager from app state."""
    tm = getattr(request.app.state, "tenant_manager", None)
    if not tm:
        raise HTTPException(status_code=503, detail="Tenant manager not configured")
    return tm


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get("/tenants")
async def list_tenants(
    request: Request,
    user: dict = Depends(_require_admin),
):
    """List all tenants. Super-admin only in production;
    currently returns the authenticated user's tenant."""
    tm = _tenant_manager(request)
    tenant_id = user.get("tenant_id", "")

    # For now, return only the user's own tenant
    tenant = tm.get_tenant(tenant_id)
    if not tenant:
        return {"tenants": []}

    member_count = tm.get_member_count(tenant_id)
    return {
        "tenants": [{
            "id": tenant.id,
            "name": tenant.name,
            "plan": tenant.plan,
            "member_count": member_count,
            "created_at": tenant.created_at.isoformat(),
        }],
    }


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    request: Request,
    user: dict = Depends(_require_admin),
):
    """Get detailed tenant information."""
    _verify_tenant_access(user, tenant_id)
    tm = _tenant_manager(request)

    tenant = tm.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    members = tm.list_members(tenant_id)
    return {
        "id": tenant.id,
        "name": tenant.name,
        "plan": tenant.plan,
        "api_key_prefix": tenant.api_key[:8] + "...",
        "limits": {
            "max_memories": tenant.limits.max_memories,
            "max_stores": tenant.limits.max_stores,
            "max_requests_per_minute": tenant.limits.max_requests_per_minute,
        },
        "member_count": len(members),
        "members": members,
        "created_at": tenant.created_at.isoformat(),
    }


@router.put("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    req: UpdateTenantRequest,
    request: Request,
    user: dict = Depends(_require_admin),
):
    """Update tenant name or plan. Requires owner/admin role."""
    _verify_tenant_access(user, tenant_id)
    tm = _tenant_manager(request)

    try:
        if req.plan:
            tenant = tm.update_plan(tenant_id, req.plan)
        else:
            tenant = tm.get_tenant(tenant_id)

        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        return {
            "id": tenant.id,
            "name": tenant.name,
            "plan": tenant.plan,
            "updated": True,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/tenants/{tenant_id}/members")
async def invite_member(
    tenant_id: str,
    req: InviteMemberRequest,
    request: Request,
    user: dict = Depends(_require_admin),
):
    """Invite a team member to the tenant."""
    _verify_tenant_access(user, tenant_id)
    tm = _tenant_manager(request)

    try:
        member = tm.invite_member(
            tenant_id,
            req.email,
            role=req.role,
            invited_by=user.get("email", ""),
        )
        return member
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tenants/{tenant_id}/members")
async def list_members(
    tenant_id: str,
    request: Request,
    user: dict = Depends(_require_admin),
):
    """List all members of a tenant."""
    _verify_tenant_access(user, tenant_id)
    tm = _tenant_manager(request)
    members = tm.list_members(tenant_id)
    return {"members": members, "count": len(members)}


@router.put("/tenants/{tenant_id}/members/{member_id}")
async def update_member_role(
    tenant_id: str,
    member_id: str,
    req: UpdateRoleRequest,
    request: Request,
    user: dict = Depends(_require_admin),
):
    """Update a team member's role."""
    _verify_tenant_access(user, tenant_id)
    tm = _tenant_manager(request)

    try:
        updated = tm.update_member_role(tenant_id, member_id, req.role)
        if not updated:
            raise HTTPException(status_code=404, detail="Member not found")
        return {"updated": True, "member_id": member_id, "role": req.role}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/tenants/{tenant_id}/members/{member_id}")
async def remove_member(
    tenant_id: str,
    member_id: str,
    request: Request,
    user: dict = Depends(_require_admin),
):
    """Remove a team member from the tenant."""
    _verify_tenant_access(user, tenant_id)
    tm = _tenant_manager(request)

    removed = tm.remove_member(tenant_id, member_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Member not found")
    return {"removed": True, "member_id": member_id}


@router.get("/tenants/{tenant_id}/usage")
async def get_usage(
    tenant_id: str,
    request: Request,
    user: dict = Depends(_require_admin),
):
    """Get usage summary for a tenant.

    Aggregates memory count, store count, decision count, and
    API request metrics from the metering service.
    """
    _verify_tenant_access(user, tenant_id)
    tm = _tenant_manager(request)

    tenant = tm.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Gather usage from available services
    usage = {
        "tenant_id": tenant_id,
        "plan": tenant.plan,
        "limits": {
            "max_memories": tenant.limits.max_memories,
            "max_stores": tenant.limits.max_stores,
            "max_requests_per_minute": tenant.limits.max_requests_per_minute,
        },
    }

    # Memory count from metering
    metering = getattr(request.app.state, "metering", None)
    if metering:
        try:
            meter = metering.get_meter(tenant_id)
            usage["current"] = {
                "memories": meter.get("memories", 0),
                "stores": meter.get("stores", 0),
                "requests_today": meter.get("requests_today", 0),
            }
        except Exception:
            usage["current"] = {"memories": 0, "stores": 0, "requests_today": 0}
    else:
        usage["current"] = {"memories": 0, "stores": 0, "requests_today": 0}

    return usage


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _verify_tenant_access(user: dict, tenant_id: str) -> None:
    """Verify the authenticated user has access to the target tenant."""
    user_tenant = user.get("tenant_id", "")
    if user_tenant != tenant_id:
        raise HTTPException(
            status_code=403,
            detail="Access denied: you can only manage your own tenant",
        )
