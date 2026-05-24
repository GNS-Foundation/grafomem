"""
GRAFOMEM auth middleware — tenant-aware Bearer token authentication.

Modes (GRAFOMEM_AUTH_MODE env var):
  - "none"  → single-tenant, no auth required, tenant = "default_namespace"
  - "token" → multi-tenant, Bearer token maps to tenant_id via GRAFOMEM_TOKENS

GRAFOMEM_TOKENS is a JSON string: {"tok_abc": "tenant_a", "tok_xyz": "tenant_b"}
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("grafomem.auth")

# Sentinel — matches the SQLite backend's NO_TENANT
DEFAULT_NAMESPACE = "default_namespace"

# Paths that bypass auth entirely (public endpoints)
_SKIP_AUTH_PATHS = frozenset({
    "/health", "/docs", "/openapi.json", "/redoc",
    "/v1/portal/signup", "/v1/portal/login",
    "/v1/cloud/billing/webhook",
})


@dataclass
class TenantContext:
    """Injected into request.state by the auth middleware."""
    tenant_id: str
    authenticated: bool


class TenantAuthMiddleware(BaseHTTPMiddleware):
    """Extract tenant_id from Bearer token, inject into request.state.

    Modes:
        none:  No auth required. All requests get tenant = DEFAULT_NAMESPACE.
        token: Requires Authorization: Bearer <token>. Token → tenant mapping
               loaded from GRAFOMEM_TOKENS env var (JSON dict).
    """

    def __init__(self, app, auth_mode: str = "none", tokens: dict[str, str] | None = None):
        super().__init__(app)
        self.auth_mode = auth_mode or os.environ.get("GRAFOMEM_AUTH_MODE", "none")
        self.tokens = tokens or self._load_tokens()
        if self.auth_mode == "token":
            logger.info("Token auth enabled (%d tokens loaded)", len(self.tokens))
        else:
            logger.info("Auth disabled (single-tenant mode)")

    @staticmethod
    def _load_tokens() -> dict[str, str]:
        raw = os.environ.get("GRAFOMEM_TOKENS", "{}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("GRAFOMEM_TOKENS is not valid JSON — no tokens loaded")
            return {}

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth for health/docs/portal/webhook/badge endpoints
        path = request.url.path
        if (path in _SKIP_AUTH_PATHS
            or path.startswith("/portal")
            or path.startswith("/v1/portal")
            or path.startswith("/v1/cloud/billing/webhook")
            or path.startswith("/v1/cloud/compliance/badge")):
            request.state.tenant = TenantContext(
                tenant_id=DEFAULT_NAMESPACE, authenticated=False
            )
            return await call_next(request)

        if self.auth_mode == "none":
            request.state.tenant = TenantContext(
                tenant_id=DEFAULT_NAMESPACE, authenticated=False
            )
            return await call_next(request)

        # Token auth — return JSONResponse directly (not HTTPException,
        # which doesn't propagate correctly from BaseHTTPMiddleware).
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Missing or malformed Authorization header. "
                              "Expected: Bearer <token>",
                },
            )

        token = auth_header[7:].strip()
        tenant_id = self.tokens.get(token)
        if tenant_id is None:
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid token. Not mapped to any tenant."},
            )

        request.state.tenant = TenantContext(
            tenant_id=tenant_id, authenticated=True
        )
        return await call_next(request)

