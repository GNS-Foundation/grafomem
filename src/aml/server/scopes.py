"""Scope enforcement for Grafomem Cloud API keys.

Grafomem uses a **flat scope model**: every API key carries a list of
fine-grained scopes (e.g. ``memory:read``, ``orchestrator:run``) that
govern which endpoints the key may call.

For backward compatibility with the original 3-role system
(``admin`` / ``agent`` / ``read_only``), the ``ROLE_SCOPES`` mapping
provides sensible defaults that are applied when a key is created
without an explicit scope list.  The ``*`` superuser scope grants
access to every endpoint and is the sole scope assigned to
``admin`` keys.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

# ── Vocabulary ───────────────────────────────────────────────────────────────

SCOPE_VOCABULARY: frozenset[str] = frozenset(
    {
        # Core GMP
        "memory:read",
        "memory:write",
        "memory:admin",
        # Orchestrator
        "orchestrator:run",
        "orchestrator:admin",
        # Governance gateway
        "governance:read",
        "governance:admin",
        # Decision trail
        "decisions:read",
        # CGR — capability-grounded reputation scores (read-only)
        "cgr:read",
        # Erasure
        "erasure:execute",
        # Gcrumbs
        "gcrumbs:read",
        # LLM provider management
        "llm:admin",
        # Webhooks
        "webhooks:admin",
        # Key management
        "keys:admin",
        # Platform admin (tenant CRUD, billing)
        "admin:platform",
        # Compliance & regulatory (reports, assurance, audit export, landing)
        "compliance:read",
        "compliance:admin",
        # Governed artifacts (artifact registry, provenance, compositions, world model)
        "artifacts:read",
        "artifacts:admin",
        # Manifold & templates
        "manifold:read",
        # CGR calibration authority (Gate-1) — privileged WRITE of agent_calibration.
        # Held ONLY by the identity authority (sim operator / GEIANT); NEVER granted to
        # any role default or agent ingestion key. A self-assignable w defeats the gate.
        "calibration:write",
        # SSO / SAML configuration
        "sso:admin",
        # Superuser
        "*",
    }
)

# ── Role → default scopes ───────────────────────────────────────────────────

ROLE_SCOPES: dict[str, list[str]] = {
    "admin": ["*"],
    "agent": [
        "memory:read",
        "memory:write",
        "orchestrator:run",
        "decisions:read",
        "cgr:read",
        "gcrumbs:read",
    ],
    "read_only": [
        "memory:read",
        "decisions:read",
        "cgr:read",
        "gcrumbs:read",
    ],
}

# ── Guards ───────────────────────────────────────────────────────────────────


def require_scope(request: Request, scope: str) -> None:
    """Raise 403 if the authenticated key doesn't carry the required scope.

    In no-auth mode (tenant_id == DEFAULT_NAMESPACE), all scopes are granted.
    The ``*`` superuser scope grants access to everything.
    """
    from aml.server.auth import DEFAULT_NAMESPACE

    ctx = getattr(request.state, "tenant", None)
    if ctx is None or ctx.tenant_id == DEFAULT_NAMESPACE:
        return  # No-auth / single-tenant mode

    # `admin:platform` is a PLATFORM-OPERATOR gate, not an ordinary scope. It is
    # satisfied ONLY by platform-operator identity (PLATFORM_TENANT_IDS) — NEVER by the
    # `*` superuser scope, and never by a tenant merely holding the literal scope. This
    # closes the cross-tenant privilege escalation on EVERY route that guards on it
    # (P0, 2026-09-11): before this, any tenant's `*`-scoped default key satisfied it.
    if scope == "admin:platform":
        require_platform(request)
        return

    scopes = getattr(ctx, "scopes", [])
    if "*" in scopes:
        return  # Superuser (for all non-platform scopes)

    if scope not in scopes:
        from fastapi import HTTPException

        raise HTTPException(403, f"Insufficient scope. Required: {scope}")


def platform_tenant_ids() -> set[str]:
    """The set of tenant ids permitted to act as PLATFORM operators.

    Sourced from the ``PLATFORM_TENANT_IDS`` env var (comma-separated). This is the
    interim allowlist; the durable form is a ``tenants.is_platform`` column added via
    the migration lane. Empty ⇒ NO tenant is a platform operator ⇒ platform routes
    fail closed for everyone until an operator id is configured.
    """
    import os

    raw = os.environ.get("PLATFORM_TENANT_IDS", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def require_platform(request: Request) -> None:
    """Raise 403 unless the caller is a PLATFORM operator.

    This is a SEPARATE gate from role/scope: platform-operator identity is defined
    ONLY by membership in ``PLATFORM_TENANT_IDS``. A ``role='admin'`` key or a
    ``scopes=['*']`` superuser key does **NOT** satisfy it — that conflation was the
    cross-tenant privilege-escalation this gate closes. Platform routes (tenant CRUD,
    cross-tenant key rotation, cross-tenant usage) MUST use this, not ``require_scope``.
    """
    from aml.server.auth import DEFAULT_NAMESPACE

    ctx = getattr(request.state, "tenant", None)
    if ctx is None or ctx.tenant_id == DEFAULT_NAMESPACE:
        return  # no-auth / single-tenant dev mode — no multi-tenant surface to protect

    if ctx.tenant_id not in platform_tenant_ids():
        from fastapi import HTTPException

        raise HTTPException(403, "Access denied: platform-operator identity required")


def require_platform_or_self(request: Request, target_tenant_id: str) -> None:
    """Allow a PLATFORM operator (any tenant) OR a tenant acting on its OWN tenant.

    Used by tenant-scoped admin routes (rotate own key, read own usage, get own
    tenant): a tenant may manage itself; only a platform operator may reach across
    tenants. Cross-tenant access by a non-platform caller ⇒ 403.
    """
    from aml.server.auth import DEFAULT_NAMESPACE

    ctx = getattr(request.state, "tenant", None)
    if ctx is None or ctx.tenant_id == DEFAULT_NAMESPACE:
        return  # no-auth / single-tenant dev mode

    if ctx.tenant_id in platform_tenant_ids():
        return  # platform operator — cross-tenant permitted
    if ctx.tenant_id == target_tenant_id:
        return  # acting on own tenant

    from fastapi import HTTPException

    raise HTTPException(403, "Access denied: platform-operator identity or own-tenant required")


# ── Tenant-admin default scope set (NOT platform, NOT superuser) ─────────────

#: Scopes granted to a new tenant's default "admin" key. Deliberately EXCLUDES the
#: superuser ``*``, the platform gate ``admin:platform``, and the identity-authority
#: ``calibration:write`` — so a tenant's own default key can fully operate that tenant
#: without being able to reach platform routes or self-assign calibration weight.
TENANT_ADMIN_SCOPES: list[str] = [
    "memory:read", "memory:write", "memory:admin",
    "orchestrator:run", "orchestrator:admin",
    "governance:read", "governance:admin",
    "decisions:read", "cgr:read", "erasure:execute", "gcrumbs:read",
    "llm:admin", "webhooks:admin", "keys:admin",
    "compliance:read", "compliance:admin",
    "artifacts:read", "artifacts:admin", "manifold:read", "sso:admin",
]


def require_store_access(request: Request, store_id: str) -> None:
    """Raise 403 if the key is store-restricted and this store isn't allowed.

    Empty *allowed_stores* means all stores are accessible.
    """
    from aml.server.auth import DEFAULT_NAMESPACE

    ctx = getattr(request.state, "tenant", None)
    if ctx is None or ctx.tenant_id == DEFAULT_NAMESPACE:
        return

    allowed = getattr(ctx, "allowed_stores", [])
    if not allowed:  # empty = all stores
        return

    if store_id not in allowed:
        from fastapi import HTTPException

        raise HTTPException(403, f"Key not authorized for store: {store_id}")


# ── Validation ───────────────────────────────────────────────────────────────


def validate_scopes(scopes: list[str]) -> list[str]:
    """Validate that all scopes are in the vocabulary. Returns cleaned list."""
    invalid = set(scopes) - SCOPE_VOCABULARY
    if invalid:
        raise ValueError(
            f"Invalid scopes: {sorted(invalid)}. "
            f"Valid: {sorted(SCOPE_VOCABULARY)}"
        )
    return sorted(set(scopes))
