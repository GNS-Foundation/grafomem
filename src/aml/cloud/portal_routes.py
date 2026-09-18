"""
GRAFOMEM portal routes — self-service tenant portal API.

Mounted at ``/v1/portal`` on the main FastAPI app.  Provides signup, login,
dashboard data, key rotation, and upgrade endpoints.  Authentication uses
JWT tokens issued by :class:`PortalAuth` — stored in the client's
``localStorage`` and sent as ``Authorization: Bearer <jwt>``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from aml.server.scopes import require_scope

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


class ChangePasswordRequest(BaseModel):
    """Authenticated password change: current password re-verified, new one set."""
    current_password: str
    new_password: str


class UpdateProfileRequest(BaseModel):
    """Editable profile fields (display name + IANA timezone; email/plan read-only)."""
    name: str
    timezone: str | None = None


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


class ApiKeyMeta(BaseModel):
    """Non-secret metadata for one tenant_api_keys row — NEVER the key itself.
    014 step (a): the console lists keys by metadata; the full key is shown only at
    mint/rotate (the create/rotate response bodies), never on /me."""
    key_id: str
    name: str | None = None
    role: str | None = None
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class DashboardResponse(BaseModel):
    """Aggregated dashboard data returned by ``GET /v1/portal/me``."""
    tenant_id: str
    name: str
    email: str
    keys: list[ApiKeyMeta] = Field(default_factory=list)  # metadata only — no usable key
    plan: str
    timezone: str | None = None
    usage: UsageSummaryOut
    compliance: ComplianceOut | None = None
    billing: BillingOut | None = None


class UpgradeRequest(BaseModel):
    """Plan upgrade payload."""
    plan: str = "pro"


class SyncRequest(BaseModel):
    """Tenant sync payload — sent after first Supabase login."""
    name: str = ""
    plan: str = "starter"


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
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    else:
        token = request.headers.get("X-API-Key", "")

    if not token:
        raise HTTPException(401, "Missing Authorization or X-API-Key header")

    pa = _portal_auth(request)
    info = pa.verify_token(token)
    if info is None:
        raise HTTPException(401, "Invalid or expired token")
        
    if info.get("role") and info.get("role") != "admin":
        raise HTTPException(403, f"Access denied: portal access requires admin role, got {info.get('role')}")
        
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


def _audit_logger(request: Request):
    return getattr(request.app.state, "audit_logger", None)


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
    try:
        pa = _portal_auth(request)
        result = pa.login(email=req.email, password=req.password)
        if result is None:
            raise HTTPException(401, "Invalid email or password")
        info, token = result
        return TokenResponse(token=token, **info)
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        err = traceback.format_exc()
        logger.error(f"Login error: {err}")
        raise HTTPException(500, f"Login crashed: {exc}")


@router.post("/sync")
async def sync_tenant(req: SyncRequest, request: Request):
    """Auto-provision a tenant after Supabase login.

    Called by the frontend after a successful Supabase auth (signup, OAuth,
    or email confirmation).  Verifies the Supabase JWT from the Authorization
    header, then finds-or-creates the linked tenant.
    """
    tenant = _require_portal_auth(request)  # verifies Supabase JWT + auto-provisions
    return {
        "tenant_id": tenant["tenant_id"],
        "name": tenant["name"],
        "email": tenant["email"],
        "plan": tenant["plan"],
    }


@router.get("/me", response_model=DashboardResponse)
async def get_dashboard(request: Request):
    """Fetch aggregated dashboard data for the authenticated tenant."""
    tenant = _require_portal_auth(request)
    tenant_id = tenant["tenant_id"]

    # Timezone preference (best-effort; column exists via ensure_schema)
    timezone = None
    try:
        pa = _portal_auth(request)
        # Bind the pooled connection to a local so the _PooledConnectionProxy stays
        # alive through fetch. Inlined as pa._get_conn().execute(...).fetchone() the
        # proxy is GC'd right after execute(), which returns+resets the connection
        # mid-query and the fetch then fails (see login/signup for the same idiom).
        conn = pa._get_conn()
        row = conn.execute(
            "SELECT timezone FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
        if row:
            timezone = row.get("timezone")
    except Exception:
        pass

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

    # API keys — METADATA ONLY (014 step a): key_id/name/role/scopes/timestamps, never
    # the key. The full key is returned solely at mint (/v1/portal/keys) and rotate
    # (/v1/portal/rotate-key).
    keys: list[ApiKeyMeta] = []
    try:
        pa = _portal_auth(request)
        # Bind to a local (see the timezone read above): the inline
        # pa._get_conn().execute(...).fetchall() form GC's the connection proxy right
        # after execute(), returning+resetting the connection before fetchall() runs,
        # which silently yields an empty key list.
        conn = pa._get_conn()
        rows = conn.execute(
            "SELECT key_id, name, role, scopes, created_at, last_used_at, expires_at "
            "FROM tenant_api_keys WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        ).fetchall()
        for r in rows:
            sc = r.get("scopes")
            keys.append(ApiKeyMeta(
                key_id=r["key_id"], name=r.get("name"), role=r.get("role"),
                scopes=(sc if isinstance(sc, list) else (json.loads(sc) if sc else [])),
                created_at=r.get("created_at"), last_used_at=r.get("last_used_at"),
                expires_at=r.get("expires_at"),
            ))
    except Exception:
        # Do NOT swallow silently: a bare `except: pass` here hid the pooled-connection
        # proxy bug (empty key list looked normal). Log with the traceback so the next
        # such failure is visible. Still non-fatal — the dashboard renders without the
        # (non-secret) key metadata rather than 500ing.
        logger.warning(
            "dashboard: failed to load API key metadata for tenant %s", tenant_id,
            exc_info=True,
        )

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
        keys=keys,
        plan=tenant["plan"],
        timezone=timezone,
        usage=usage,
        compliance=compliance,
        billing=billing,
    )


class CreateApiKeyRequest(BaseModel):
    name: str = "custom_key"
    role: str = "admin"
    expires_at: int | None = None  # Unix timestamp
    ip_allowlist: list[str] | None = None

@router.post("/api-keys")
async def create_api_key(req: CreateApiKeyRequest, request: Request):
    """Generate a new scoped API key."""
    tenant = _require_portal_auth(request)
    mgr = _tenant_manager(request)
    audit = _audit_logger(request)
    try:
        from datetime import datetime, timezone
        exp_dt = datetime.fromtimestamp(req.expires_at, tz=timezone.utc) if req.expires_at else None
        key_info = mgr.create_api_key(
            tenant["tenant_id"], 
            name=req.name, 
            role=req.role,
            expires_at=exp_dt,
            ip_allowlist=req.ip_allowlist
        )
        if audit:
            audit.log(
                tenant_id=tenant["tenant_id"],
                actor=tenant["email"],
                action="create_api_key",
                resource="api_keys",
                metadata={"name": req.name, "role": req.role}
            )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return key_info

@router.post("/profile")
async def update_profile(req: UpdateProfileRequest, request: Request):
    """Update the signed-in user's editable profile (display name). Auth: portal
    session; acts only on the caller's tenant. Email/plan are read-only."""
    tenant = _require_portal_auth(request)
    audit = _audit_logger(request)
    pa = _portal_auth(request)
    try:
        result = pa.update_profile(tenant["tenant_id"], name=req.name, timezone=req.timezone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"update-profile error: {exc}")
        raise HTTPException(500, "Profile update failed")
    if audit:
        try:
            audit.log(tenant_id=tenant["tenant_id"], actor=tenant["email"],
                      action="update_profile", resource="account")
        except Exception:
            pass
    return {"status": "ok", **result}


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, request: Request):
    """Change the signed-in user's password. Re-verifies the current password before
    setting the new one. Auth: portal session (JWT); acts only on the caller's tenant."""
    tenant = _require_portal_auth(request)
    audit = _audit_logger(request)
    pa = _portal_auth(request)
    try:
        pa.change_password(tenant["tenant_id"], req.current_password, req.new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"change-password error: {exc}")
        raise HTTPException(500, "Password change failed")
    if audit:
        try:
            audit.log(tenant_id=tenant["tenant_id"], actor=tenant["email"],
                      action="change_password", resource="account")
        except Exception:
            pass
    return {"status": "ok"}


@router.post("/rotate-key")
async def rotate_key(request: Request):
    """Revoke the current API key and issue a new one."""
    tenant = _require_portal_auth(request)
    mgr = _tenant_manager(request)
    audit = _audit_logger(request)
    tenant_id = tenant["tenant_id"]
    try:
        conn = mgr._get_conn()
        conn.execute("DELETE FROM tenant_api_keys WHERE tenant_id = %s", (tenant_id,))
        new_key = mgr.create_api_key(tenant_id, name="default_admin", role="admin")
        # 014 step (a): the tenants.api_key sync is REMOVED. /v1/portal/me reads
        # tenant_api_keys metadata now, not tenants.api_key, so there is nothing to keep
        # in sync; the new key lives only in tenant_api_keys and this response body.
        # Deleting the rows is not revocation on its own: the auth middleware
        # caches key resolutions for 60s. Evict them or the old key keeps working.
        from aml.server.auth import invalidate_tenant_key_cache
        invalidate_tenant_key_cache(request, tenant_id)
        if audit:
            audit.log(
                tenant_id=tenant_id,
                actor=tenant["email"],
                action="rotate_api_key",
                resource="api_keys",
            )
    except Exception as e:
        raise HTTPException(500, f"Error rotating keys: {e}")
    return {"api_key": new_key["api_key"]}

@router.get("/audit")
async def get_audit_logs(request: Request, limit: int = 100):
    """Retrieve immutable audit logs for the tenant."""
    tenant = _require_portal_auth(request)
    audit = _audit_logger(request)
    if not audit:
        return {"logs": []}
    
    logs = audit.get_logs(tenant["tenant_id"], limit=limit)
    return {"logs": logs}


@router.post("/upgrade")
async def upgrade(req: UpgradeRequest, request: Request):
    """Initiate Stripe Checkout for a plan upgrade."""
    tenant = _require_portal_auth(request)
    sb = _stripe_billing(request)

    if sb is None:
        raise HTTPException(503, "Billing not configured — set STRIPE_SECRET_KEY")

    try:
        url = sb.create_checkout_session(
            tenant_id=tenant["tenant_id"],
            plan=req.plan,
            success_url="https://cloud.grafomem.com/settings?upgraded=true",
            cancel_url="https://cloud.grafomem.com/settings",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(502, f"Stripe checkout failed: {exc}")

    return {"checkout_url": url}


@router.post("/portal")
async def portal(request: Request):
    """Initiate Stripe Customer Portal session."""
    tenant = _require_portal_auth(request)
    sb = _stripe_billing(request)

    if sb is None:
        raise HTTPException(503, "Billing not configured")

    try:
        url = sb.create_portal_session(
            tenant_id=tenant["tenant_id"],
            return_url="https://cloud.grafomem.com/settings",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Stripe portal failed: {exc}")

    return {"portal_url": url}

