"""
GRAFOMEM Webhook API — REST endpoints for webhook management.

Provides CRUD for webhook configurations, delivery history, and test dispatch.
Mounted at /v1/webhooks when Cloud mode is active.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("grafomem.cloud.webhook_routes")

from aml.cloud.schemas import (
    WebhookConfigResponse,
    WebhookCreatedResponse,
    WebhookListResponse,
    WebhookEventTypesResponse,
)


# ============================================================================
# Pydantic models
# ============================================================================

class RegisterWebhookRequest(BaseModel):
    url: str
    events: list[str]
    description: str = ""


class UpdateWebhookRequest(BaseModel):
    url: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None
    description: str | None = None


# ============================================================================
# Helper
# ============================================================================

def _get_tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


# ============================================================================
# Router factory
# ============================================================================

def create_webhook_router(webhook_service) -> APIRouter:
    """Create the Webhook management FastAPI router."""

    router = APIRouter(prefix="/v1/webhooks", tags=["Webhooks"])

    # ------------------------------------------------------------------
    # POST /v1/webhooks — register a webhook
    # ------------------------------------------------------------------

    @router.post("/", status_code=201, response_model=WebhookCreatedResponse)
    async def register_webhook(req: RegisterWebhookRequest, request: Request):
        """Register a new webhook endpoint.

        Returns the webhook config **including the signing secret** (shown
        only once at creation time).
        """
        tenant_id = _get_tenant_id(request)

        try:
            config = webhook_service.register(
                tenant_id=tenant_id,
                url=req.url,
                events=req.events,
                description=req.description,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))

        # Include secret only on creation
        result = webhook_service.config_to_dict(config)
        result["secret"] = config.secret
        return result

    # ------------------------------------------------------------------
    # GET /v1/webhooks — list webhooks
    # ------------------------------------------------------------------

    @router.get("/", response_model=WebhookListResponse)
    async def list_webhooks(request: Request):
        """List all webhooks for the tenant."""
        tenant_id = _get_tenant_id(request)
        configs = webhook_service.list_webhooks(tenant_id)
        return {
            "webhooks": [webhook_service.config_to_dict(c) for c in configs],
            "count": len(configs),
        }

    # ------------------------------------------------------------------
    # GET /v1/webhooks/{webhook_id} — get webhook
    # ------------------------------------------------------------------

    @router.get("/{webhook_id}", response_model=WebhookConfigResponse)
    async def get_webhook(webhook_id: str, request: Request):
        """Get a single webhook configuration."""
        tenant_id = _get_tenant_id(request)
        config = webhook_service.get_webhook(webhook_id)

        if config is None or config.tenant_id != tenant_id:
            raise HTTPException(404, f"Webhook '{webhook_id}' not found")

        return webhook_service.config_to_dict(config)

    # ------------------------------------------------------------------
    # PUT /v1/webhooks/{webhook_id} — update webhook
    # ------------------------------------------------------------------

    @router.put("/{webhook_id}", response_model=WebhookConfigResponse)
    async def update_webhook(
        webhook_id: str,
        req: UpdateWebhookRequest,
        request: Request,
    ):
        """Update webhook configuration."""
        tenant_id = _get_tenant_id(request)

        try:
            config = webhook_service.update_webhook(
                webhook_id=webhook_id,
                tenant_id=tenant_id,
                url=req.url,
                events=req.events,
                enabled=req.enabled,
                description=req.description,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))

        if config is None:
            raise HTTPException(404, f"Webhook '{webhook_id}' not found")

        return webhook_service.config_to_dict(config)

    # ------------------------------------------------------------------
    # DELETE /v1/webhooks/{webhook_id} — delete webhook
    # ------------------------------------------------------------------

    @router.delete("/{webhook_id}")
    async def delete_webhook(webhook_id: str, request: Request):
        """Delete a webhook configuration."""
        tenant_id = _get_tenant_id(request)
        deleted = webhook_service.delete_webhook(webhook_id, tenant_id)

        if not deleted:
            raise HTTPException(404, f"Webhook '{webhook_id}' not found")

        return {"deleted": True, "webhook_id": webhook_id}

    # ------------------------------------------------------------------
    # GET /v1/webhooks/{webhook_id}/deliveries — delivery history
    # ------------------------------------------------------------------

    @router.get("/{webhook_id}/deliveries")
    async def get_deliveries(
        webhook_id: str,
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        """Get delivery history for a webhook."""
        tenant_id = _get_tenant_id(request)
        config = webhook_service.get_webhook(webhook_id)

        if config is None or config.tenant_id != tenant_id:
            raise HTTPException(404, f"Webhook '{webhook_id}' not found")

        deliveries = webhook_service.get_deliveries(
            webhook_id, limit=limit, offset=offset,
        )
        return {
            "deliveries": [webhook_service.delivery_to_dict(d) for d in deliveries],
            "count": len(deliveries),
        }

    # ------------------------------------------------------------------
    # POST /v1/webhooks/{webhook_id}/test — send test event
    # ------------------------------------------------------------------

    @router.post("/{webhook_id}/test")
    async def test_webhook(webhook_id: str, request: Request):
        """Send a test event to a webhook endpoint."""
        tenant_id = _get_tenant_id(request)
        delivery = webhook_service.send_test(webhook_id, tenant_id)

        if delivery is None:
            raise HTTPException(404, f"Webhook '{webhook_id}' not found")

        return {
            "test": True,
            "delivery": webhook_service.delivery_to_dict(delivery),
        }

    # ------------------------------------------------------------------
    # GET /v1/webhooks/events — list valid event types
    # ------------------------------------------------------------------

    @router.get("/events/types", response_model=WebhookEventTypesResponse)
    async def list_event_types():
        """List all valid webhook event types."""
        from aml.cloud.webhook_service import VALID_EVENT_TYPES
        return {
            "event_types": sorted(VALID_EVENT_TYPES),
        }

    return router
