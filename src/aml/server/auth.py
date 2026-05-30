"""
GRAFOMEM auth middleware — tenant-aware Bearer token authentication.

Modes (GRAFOMEM_AUTH_MODE env var):
  - "none"  → single-tenant, no auth required, tenant = "default_namespace"
  - "token" → multi-tenant, Bearer token maps to tenant_id via GRAFOMEM_TOKENS
  - "cloud" → multi-tenant, X-API-Key resolved from the tenants DB table

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
    "/health", "/healthz", "/readyz", "/metrics",
    "/docs", "/openapi.json", "/redoc",
    "/v1/portal/signup", "/v1/portal/login",
    "/v1/cloud/billing/webhook",
})


@dataclass
class TenantContext:
    """Injected into request.state by the auth middleware."""
    tenant_id: str
    authenticated: bool


class TenantAuthMiddleware(BaseHTTPMiddleware):
    """Extract tenant_id from Bearer token or X-API-Key, inject into request.state.

    Modes:
        none:  No auth required. All requests get tenant = DEFAULT_NAMESPACE.
        token: Requires Authorization: Bearer <token>. Token → tenant mapping
               loaded from GRAFOMEM_TOKENS env var (JSON dict).
        cloud: Requires X-API-Key header. Key → tenant_id resolved from
               the PostgreSQL tenants table.
    """

    def __init__(self, app, auth_mode: str = "none",
                 tokens: dict[str, str] | None = None,
                 db_url: str | None = None):
        super().__init__(app)
        self.auth_mode = auth_mode or os.environ.get("GRAFOMEM_AUTH_MODE", "none")
        self.tokens = tokens or self._load_tokens()
        self._db_url = db_url
        # In-memory cache for API key → tenant_id lookups (populated lazily)
        self._api_key_cache: dict[str, str] = {}
        if self.auth_mode == "token":
            logger.info("Token auth enabled (%d tokens loaded)", len(self.tokens))
        elif self.auth_mode == "cloud":
            logger.info("Cloud auth enabled (API keys resolved from DB)")
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

    def _resolve_api_key(self, api_key: str) -> str | None:
        """Resolve an API key to a tenant_id using the DB. Caches results."""
        if api_key in self._api_key_cache:
            return self._api_key_cache[api_key]

        if not self._db_url:
            return None

        try:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self._db_url, row_factory=dict_row,
                                   autocommit=True)
            row = conn.execute(
                "SELECT id FROM tenants WHERE api_key = %s",
                (api_key,),
            ).fetchone()
            conn.close()
            if row:
                tenant_id = row["id"]
                self._api_key_cache[api_key] = tenant_id
                return tenant_id
        except Exception as e:
            logger.warning("API key lookup failed: %s", e)
        return None

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

        # Cloud mode: resolve X-API-Key from the tenants table
        if self.auth_mode == "cloud":
            api_key = request.headers.get("X-API-Key", "")
            if not api_key:
                # Fall back to Authorization: Bearer
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    api_key = auth_header[7:].strip()

            if not api_key:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing X-API-Key or Authorization header."},
                )

            tenant_id = self._resolve_api_key(api_key)
            if tenant_id is None:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid API key."},
                )

            request.state.tenant = TenantContext(
                tenant_id=tenant_id, authenticated=True
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
