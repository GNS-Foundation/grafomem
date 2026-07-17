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
import time
from dataclasses import dataclass, field

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("grafomem.auth")

# Sentinel — matches the SQLite backend's NO_TENANT
DEFAULT_NAMESPACE = "default_namespace"

# Paths that bypass auth entirely (public endpoints)
_SKIP_AUTH_PATHS = frozenset({
    "/health", "/healthz", "/readyz", "/metrics", "/observability/metrics",
    "/docs", "/openapi.json", "/redoc",
    "/v1/portal/signup", "/v1/portal/login",
    "/v1/cloud/billing/webhook",
    "/v1/gcrumbs/public_key",
})


@dataclass
class TenantContext:
    """Injected into request.state by the auth middleware."""
    tenant_id: str
    authenticated: bool
    role: str = "admin"
    scopes: list[str] = field(default_factory=list)
    allowed_stores: list[str] = field(default_factory=list)
    key_id: str | None = None


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
        # TTL cache for API key lookups: key → (tenant_id, role, scopes, allowed_stores, key_id, ip_allowlist, cached_at)
        self._api_key_cache: dict[str, tuple] = {}
        self._cache_ttl = 60  # seconds
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

    def invalidate_cache(self, api_key: str) -> None:
        """Remove a key from the TTL cache (e.g. on revocation)."""
        self._api_key_cache.pop(api_key, None)

    def _resolve_api_key(self, api_key: str) -> tuple[str, str, list[str], list[str], str | None, list[str]] | None:
        """Resolve an API key to (tenant_id, role, scopes, allowed_stores, key_id, ip_allowlist) using the DB.

        Results are cached with a 60-second TTL.
        """
        cached = self._api_key_cache.get(api_key)
        if cached is not None:
            tenant_id, role, scopes, allowed_stores, key_id, ip_allowlist, cached_at = cached
            if time.monotonic() - cached_at < self._cache_ttl:
                return tenant_id, role, scopes, allowed_stores, key_id, ip_allowlist
            # Stale — remove and re-resolve
            del self._api_key_cache[api_key]

        if not self._db_url:
            return None

        try:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self._db_url, row_factory=dict_row,
                                   autocommit=True)
            row = conn.execute(
                "SELECT key_id, tenant_id, role, scopes, allowed_stores, expires_at, ip_allowlist "
                "FROM tenant_api_keys WHERE api_key = %s",
                (api_key,),
            ).fetchone()

            is_legacy = False
            # Fallback for legacy schema
            if not row:
                row = conn.execute(
                    "SELECT id as tenant_id, 'admin' as role FROM tenants WHERE api_key = %s",
                    (api_key,),
                ).fetchone()
                is_legacy = True

            if row:
                # Check expiry (column may not exist yet)
                from datetime import datetime, timezone
                expires_at = row.get("expires_at")
                if expires_at is not None and expires_at < datetime.now(timezone.utc):
                    conn.close()
                    return None

                # Fire-and-forget last_used_at update
                key_id_val = row.get("key_id")
                if key_id_val:
                    try:
                        conn.execute(
                            "UPDATE tenant_api_keys SET last_used_at = NOW() WHERE key_id = %s",
                            (key_id_val,),
                        )
                    except Exception:
                        pass  # best-effort, don't block auth

                conn.close()

                tenant_id = row["tenant_id"]
                role = row.get("role", "admin")

                if is_legacy:
                    scopes: list[str] = ["*"]
                    allowed_stores: list[str] = []
                    key_id_out: str | None = None
                    ip_allowlist: list[str] = []
                else:
                    # scopes / allowed_stores columns may not exist pre-migration
                    db_scopes = row.get("scopes")
                    if db_scopes:
                        scopes = db_scopes if isinstance(db_scopes, list) else json.loads(db_scopes)
                    else:
                        from aml.server.scopes import ROLE_SCOPES
                        scopes = ROLE_SCOPES.get(role, ["*"])
                    db_stores = row.get("allowed_stores")
                    if db_stores:
                        allowed_stores = db_stores if isinstance(db_stores, list) else json.loads(db_stores)
                    else:
                        allowed_stores = []
                    key_id_out = row.get("key_id")
                    db_ip = row.get("ip_allowlist")
                    if db_ip:
                        ip_allowlist = db_ip if isinstance(db_ip, list) else json.loads(db_ip)
                    else:
                        ip_allowlist = []

                self._api_key_cache[api_key] = (
                    tenant_id, role, scopes, allowed_stores, key_id_out, ip_allowlist, time.monotonic(),
                )
                return tenant_id, role, scopes, allowed_stores, key_id_out, ip_allowlist
            else:
                conn.close()
        except Exception as e:
            logger.warning("API key lookup failed: %s", e)
        return None

    def _resolve_jwt(self, token: str) -> tuple[str, str, list[str], list[str], str | None, list[str]] | None:
        """Try to resolve a portal JWT (Supabase or legacy) to a tenant_id.

        Uses the PortalAuth instance already on app.state (which shares the
        same JWT secret that issued the token).
        """
        try:
            # Access the PortalAuth instance stored on app.state during startup
            portal_auth = getattr(self.app, 'state', None)
            if portal_auth:
                portal_auth = getattr(portal_auth, 'portal_auth', None)
            if not portal_auth:
                # Fallback: create one (won't work for legacy JWTs unless
                # GRAFOMEM_PORTAL_SECRET env var is set)
                from aml.cloud.portal_auth import PortalAuth
                portal_auth = PortalAuth(db_url=self._db_url)
            info = portal_auth.verify_token(token)
            if info and info.get("tenant_id"):
                tenant_id = info["tenant_id"]
                role = info.get("role", "admin")
                scopes = ["*"]  # portal JWTs get full access
                allowed_stores: list[str] = []
                ip_allowlist: list[str] = []
                # Cache so subsequent calls with the same JWT are fast
                self._api_key_cache[token] = (
                    tenant_id, role, scopes, allowed_stores, None, ip_allowlist, time.monotonic(),
                )
                return tenant_id, role, scopes, allowed_stores, None, ip_allowlist
        except Exception as e:
            logger.debug("JWT resolution failed: %s", e)
        return None

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # CORS preflight: let OPTIONS pass through so CORSMiddleware handles it
        if request.method == "OPTIONS":
            return await call_next(request)

        # Skip auth for health/docs/portal/webhook/badge endpoints
        path = request.url.path
        # Note: Bypassing auth for prefixes silently unprotects any future
        # routes added under them. This bypass is necessary for self-authenticated
        # endpoints like /attest and the Inbox, but sensitive routes (like /verify)
        # must be explicitly carved out to enforce API-key scopes.
        if (path in _SKIP_AUTH_PATHS
            or path.startswith("/portal")
            or path.startswith("/v1/portal")
            or path.startswith("/v1/cloud/billing/webhook")
            or path.startswith("/v1/cloud/compliance/badge")
            or (path.startswith("/v1/hitl/requests/") and not path.endswith("/verify"))
            or path.startswith("/v1/hitl/approvers/")):
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
                # Fall back to query param for EventSource / WebSocket support
                api_key = request.query_params.get("token", "")

            if not api_key:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing X-API-Key, Authorization header, or token query param."},
                )

            resolved = self._resolve_api_key(api_key)

            # Fallback: try decoding as a portal JWT token
            if resolved is None:
                resolved = self._resolve_jwt(api_key)

            if resolved is None:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid API key."},
                )

            tenant_id, role, scopes, allowed_stores, key_id, ip_allowlist = resolved

            # IP allowlist enforcement
            if ip_allowlist:
                client_ip = request.client.host if request.client else None
                if client_ip and client_ip not in ip_allowlist:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": f"IP {client_ip} not in allowlist."},
                    )

            request.state.tenant = TenantContext(
                tenant_id=tenant_id, authenticated=True, role=role,
                scopes=scopes, allowed_stores=allowed_stores, key_id=key_id,
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
            # Also check X-API-Key header (portal uses this)
            api_key = request.headers.get("X-API-Key", "")
            if not api_key:
                # Finally, check query param
                api_key = request.query_params.get("token", "")
            if not api_key:
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Missing or malformed Authorization header. "
                                  "Expected: Bearer <token>"
                    },
                )
            token = api_key
        else:
            token = auth_header[7:].strip()

        resolved = None
        
        tenant_id = self.tokens.get(token)
        role = "admin"
        if tenant_id:
            resolved = (tenant_id, role, ["*"], [], None, [])

        # Fallback: try resolving as a DB API key (portal-issued keys)
        if resolved is None and self._db_url:
            resolved = self._resolve_api_key(token)

        # Fallback: try resolving as a portal JWT
        if resolved is None and self._db_url:
            resolved = self._resolve_jwt(token)

        if resolved is None:
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid token. Not mapped to any tenant."},
            )

        tenant_id, role, scopes, allowed_stores, key_id, ip_allowlist = resolved

        # IP allowlist enforcement (token mode)
        if ip_allowlist:
            client_ip = request.client.host if request.client else None
            if client_ip and client_ip not in ip_allowlist:
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"IP {client_ip} not in allowlist."},
                )

        request.state.tenant = TenantContext(
            tenant_id=tenant_id, authenticated=True, role=role,
            scopes=scopes, allowed_stores=allowed_stores, key_id=key_id,
        )
        return await call_next(request)
