"""
GRAFOMEM SSO Routes — OAuth2/OIDC endpoints for the Cloud Portal.

Provides authorization initiation, callback handling, and provider
configuration endpoints.  Mounted at /v1/portal/sso when Cloud mode
is active.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger("grafomem.cloud.sso_routes")

from aml.cloud.schemas import (
    SSOProviderListResponse,
    SSOConfiguredResponse,
)


# ============================================================================
# Pydantic models
# ============================================================================

class ConfigureProviderRequest(BaseModel):
    provider: str
    client_id: str
    client_secret: str
    issuer_url: str = ""


# ============================================================================
# Router factory
# ============================================================================

def create_sso_router(sso_provider) -> APIRouter:
    """Create the SSO authentication FastAPI router."""

    router = APIRouter(prefix="/v1/portal/sso", tags=["SSO"])

    # ------------------------------------------------------------------
    # GET /v1/portal/sso/providers — list available providers
    # ------------------------------------------------------------------

    @router.get("/providers", response_model=SSOProviderListResponse)
    async def list_providers():
        """List available SSO providers."""
        return {
            "providers": sso_provider.list_providers(),
        }

    # ------------------------------------------------------------------
    # GET /v1/portal/sso/authorize — start OAuth flow
    # ------------------------------------------------------------------

    @router.get("/authorize")
    async def authorize(provider: str = Query(...)):
        """Initiate an OAuth2/OIDC authorization flow.

        Redirects the user to the identity provider's login page.
        """
        try:
            auth_url = sso_provider.initiate_flow(provider)
        except ValueError as e:
            raise HTTPException(400, str(e))

        return RedirectResponse(url=auth_url, status_code=302)

    # ------------------------------------------------------------------
    # GET /v1/portal/sso/callback — handle OAuth callback
    # ------------------------------------------------------------------

    @router.get("/callback")
    async def callback(
        code: str = Query(...),
        state: str = Query(...),
    ):
        """Handle the OAuth2 callback from the identity provider.

        Exchanges the authorization code for tokens, resolves the user,
        and returns a GRAFOMEM JWT.
        """
        try:
            result = sso_provider.handle_callback(code, state)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            logger.error("SSO callback failed: %s", e)
            raise HTTPException(500, f"SSO authentication failed: {e}")

        # For browser flows, redirect to portal with token
        # For API flows, return JSON
        return result

    # ------------------------------------------------------------------
    # POST /v1/portal/sso/configure — admin: configure provider
    # ------------------------------------------------------------------

    @router.post("/configure", status_code=201, response_model=SSOConfiguredResponse)
    async def configure_provider(
        req: ConfigureProviderRequest,
        request: Request,
    ):
        """Configure an SSO provider (admin operation).

        Requires authentication. Stores the OAuth client credentials
        for the specified provider.
        """
        # Simple admin check — requires authenticated request
        ctx = getattr(request.state, "tenant", None)
        if ctx is None or not ctx.authenticated:
            raise HTTPException(401, "Authentication required")

        try:
            config = sso_provider.configure_provider(
                provider=req.provider,
                client_id=req.client_id,
                client_secret=req.client_secret,
                issuer_url=req.issuer_url,
            )
        except Exception as e:
            raise HTTPException(400, str(e))

        return {
            "config_id": config.config_id,
            "provider": config.provider,
            "enabled": config.enabled,
            "created_at": config.created_at.isoformat(),
        }

    return router
