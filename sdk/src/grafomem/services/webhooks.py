"""Webhook management service.

Register, list, and manage webhook endpoints for push notifications.

Usage::

    webhook = client.webhooks.register(
        url="https://example.com/grafomem-events",
        events=["governance.denied", "workflow.completed"],
    )
    print(f"Secret: {webhook['secret']}")

    deliveries = client.webhooks.deliveries(webhook["webhook_id"])
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class WebhooksService:
    """Webhook CRUD and delivery history."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    def register(
        self,
        url: str,
        events: list[str],
        description: str = "",
    ) -> dict[str, Any]:
        """Register a new webhook endpoint.

        Args:
            url: HTTPS URL to receive POST events.
            events: Event types to subscribe to.
            description: Optional human-readable description.

        Returns:
            Webhook config **including the signing secret** (shown once).
        """
        return self._http.post("/v1/webhooks/", json={
            "url": url,
            "events": events,
            "description": description,
        })

    def list(self) -> list[dict[str, Any]]:
        """List all webhooks for the tenant."""
        data = self._http.get("/v1/webhooks/")
        return data.get("webhooks", []) if isinstance(data, dict) else data

    def get(self, webhook_id: str) -> dict[str, Any]:
        """Get a single webhook configuration."""
        return self._http.get(f"/v1/webhooks/{webhook_id}")

    def update(
        self,
        webhook_id: str,
        url: str | None = None,
        events: list[str] | None = None,
        enabled: bool | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update webhook configuration."""
        body: dict[str, Any] = {}
        if url is not None:
            body["url"] = url
        if events is not None:
            body["events"] = events
        if enabled is not None:
            body["enabled"] = enabled
        if description is not None:
            body["description"] = description
        return self._http.put(f"/v1/webhooks/{webhook_id}", json=body)

    def delete(self, webhook_id: str) -> dict[str, Any]:
        """Delete a webhook configuration."""
        return self._http.delete(f"/v1/webhooks/{webhook_id}")

    def deliveries(
        self,
        webhook_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get delivery history for a webhook."""
        data = self._http.get(
            f"/v1/webhooks/{webhook_id}/deliveries",
            params={"limit": limit, "offset": offset},
        )
        return data.get("deliveries", []) if isinstance(data, dict) else data

    def test(self, webhook_id: str) -> dict[str, Any]:
        """Send a test event to a webhook."""
        return self._http.post(f"/v1/webhooks/{webhook_id}/test")

    def event_types(self) -> list[str]:
        """List all valid webhook event types."""
        data = self._http.get("/v1/webhooks/events/types")
        return data.get("event_types", []) if isinstance(data, dict) else data
