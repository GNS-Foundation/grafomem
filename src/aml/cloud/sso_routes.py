"""
GRAFOMEM SSO Routes — OAuth2/OIDC endpoints for the Cloud Portal.

Provides authorization initiation, callback handling, and provider
configuration endpoints.  Mounted at /v1/portal/sso when Cloud mode
is active.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger("grafomem.cloud.sso_routes")

# Frontend URL for post-auth redirects.  Distinct from redirect_base
# which is the *backend* URL used for the OAuth2 callback URI.
_FRONTEND_URL = os.environ.get(
    "GRAFOMEM_FRONTEND_URL",
    "https://cloud.grafomem.com",
).rstrip("/")

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
        and returns a GRAFOMEM JWT.  For browser flows, redirects to the
        portal with the token in query parameters.
        """
        try:
            result = sso_provider.handle_callback(code, state)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            logger.error("SSO callback failed: %s", e)
            raise HTTPException(500, f"SSO authentication failed: {e}")

        # Browser flow: redirect to frontend with auth params
        if isinstance(result, dict) and result.get("token"):
            from urllib.parse import urlencode
            # Redirect to the frontend /login page with params the Next.js
            # AuthProvider expects: api_key, tenant_id, name, email, plan.
            # _FRONTEND_URL is the frontend (cloud.grafomem.com), NOT the
            # backend redirect_base used for OAuth callback URIs.
            params = urlencode({
                "api_key": result.get("api_key", ""),
                "tenant_id": result.get("tenant_id", ""),
                "name": result.get("name", ""),
                "email": result.get("email", ""),
                "plan": result.get("plan", "starter"),
            })
            return RedirectResponse(
                url=f"{_FRONTEND_URL}/login?{params}", status_code=302,
            )

        # API flow: return JSON
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

        Requires authentication via Bearer JWT token. Stores the OAuth
        client credentials for the specified provider.
        """
        # /v1/portal/* paths bypass auth middleware → verify JWT directly
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(401, "Authentication required — provide Authorization: Bearer <token>")
        token = auth_header[7:].strip()
        portal_auth = getattr(request.app.state, "portal_auth", None)
        if portal_auth is None:
            raise HTTPException(500, "Portal auth not available")
        info = portal_auth.verify_token(token)
        if not info or not info.get("tenant_id"):
            raise HTTPException(401, "Invalid or expired token")

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

    # ------------------------------------------------------------------
    # SAML 2.0 endpoints
    # ------------------------------------------------------------------

    @router.get("/saml/metadata")
    async def saml_metadata():
        """Download SP metadata XML for SAML 2.0 configuration.

        This endpoint returns the GRAFOMEM Service Provider metadata
        that the enterprise IdP admin needs to import.
        """
        from fastapi.responses import Response
        xml = sso_provider.get_sp_metadata()
        return Response(
            content=xml,
            media_type="application/xml",
            headers={"Content-Disposition": "inline; filename=grafomem-sp-metadata.xml"},
        )

    @router.post("/saml/configure", status_code=201)
    async def configure_saml(
        request: Request,
        metadata_url: str = "",
        metadata_xml: str = "",
        sp_entity_id: str = "",
    ):
        """Configure a SAML 2.0 Identity Provider (admin operation).

        Accepts either ``metadata_url`` (auto-discovery) or
        ``metadata_xml`` (raw XML paste).  Requires authentication.
        """
        ctx = getattr(request.state, "tenant", None)
        if ctx is None or not ctx.authenticated:
            raise HTTPException(401, "Authentication required")

        # Accept form data or JSON body
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass

        m_url = body.get("metadata_url") or metadata_url
        m_xml = body.get("metadata_xml") or metadata_xml
        s_entity = body.get("sp_entity_id") or sp_entity_id
        attr_map = body.get("attribute_mapping")

        try:
            config = sso_provider.configure_saml(
                metadata_url=m_url or None,
                metadata_xml=m_xml or None,
                sp_entity_id=s_entity or None,
                attribute_mapping=attr_map,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            logger.error("SAML configure failed: %s", e)
            raise HTTPException(500, f"SAML configuration failed: {e}")

        return {
            "config_id": config.config_id,
            "idp_entity_id": config.idp_entity_id,
            "sp_entity_id": config.sp_entity_id,
            "enabled": config.enabled,
        }

    @router.get("/saml/login")
    async def saml_login():
        """Initiate a SAML 2.0 SP-initiated SSO flow.

        Redirects the user to the configured IdP login page.
        """
        try:
            redirect_url = sso_provider.initiate_saml_flow()
        except ValueError as e:
            raise HTTPException(400, str(e))
        return RedirectResponse(url=redirect_url, status_code=302)

    @router.post("/saml/acs")
    async def saml_acs(request: Request):
        """SAML 2.0 Assertion Consumer Service (ACS).

        Receives the SAML Response via HTTP-POST binding from the IdP.
        Extracts the user identity and issues a GRAFOMEM JWT.
        """
        form = await request.form()
        saml_response = form.get("SAMLResponse", "")
        relay_state = form.get("RelayState", "")

        if not saml_response:
            raise HTTPException(400, "Missing SAMLResponse parameter")

        try:
            result = sso_provider.handle_saml_response(
                saml_response=saml_response,
                relay_state=relay_state,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            logger.error("SAML ACS failed: %s", e)
            raise HTTPException(500, f"SAML authentication failed: {e}")

        # Redirect to frontend with auth params
        if isinstance(result, dict) and result.get("token"):
            from urllib.parse import urlencode
            params = urlencode({
                "api_key": result.get("api_key", ""),
                "tenant_id": result.get("tenant_id", ""),
                "name": result.get("name", ""),
                "email": result.get("email", ""),
                "plan": result.get("plan", "starter"),
            })
            return RedirectResponse(
                url=f"{_FRONTEND_URL}/login?{params}", status_code=302,
            )

        return result

    return router
