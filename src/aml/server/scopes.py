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
        "gcrumbs:read",
    ],
    "read_only": [
        "memory:read",
        "decisions:read",
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

    scopes = getattr(ctx, "scopes", [])
    if "*" in scopes:
        return  # Superuser

    if scope not in scopes:
        from fastapi import HTTPException

        raise HTTPException(403, f"Insufficient scope. Required: {scope}")


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
