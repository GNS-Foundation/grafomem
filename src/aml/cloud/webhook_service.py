"""
GRAFOMEM Webhook Service — push notifications for governance events.

Dispatches HTTP POST webhooks with HMAC-SHA256 signed payloads when
governance denials, HITL escalations, workflow completions, or erasure
events occur.  Delivery uses retry with exponential backoff.

Tables:
  - ``webhook_configs``: tenant's registered webhook URLs + event filters
  - ``webhook_deliveries``: delivery log with status tracking

Event Types:
  - ``governance.denied``    — a policy blocked an operation
  - ``governance.escalated`` — a policy triggered HITL escalation
  - ``workflow.completed``   — a workflow finished successfully
  - ``workflow.error``       — a workflow failed with an error
  - ``erasure.issued``       — a GDPR erasure certificate was issued
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.webhooks")


# ============================================================================
# Constants
# ============================================================================

VALID_EVENT_TYPES = frozenset({
    "governance.denied",
    "governance.escalated",
    "workflow.completed",
    "workflow.error",
    "erasure.issued",
})

# Retry delays in seconds (exponential backoff)
_RETRY_DELAYS = [1, 5, 30]
_MAX_ATTEMPTS = len(_RETRY_DELAYS) + 1  # initial + retries


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"


# ============================================================================
# Data types
# ============================================================================

@dataclass(slots=True)
class WebhookConfig:
    """A registered webhook endpoint."""
    webhook_id: str
    tenant_id: str
    url: str
    events: list[str]
    secret: str           # HMAC-SHA256 signing secret
    enabled: bool = True
    description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class WebhookDelivery:
    """A delivery attempt record."""
    delivery_id: str
    webhook_id: str
    tenant_id: str
    event_type: str
    payload: dict[str, Any]
    status: DeliveryStatus
    attempts: int = 0
    response_code: int | None = None
    response_body: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS webhook_configs (
    webhook_id   TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    url          TEXT NOT NULL,
    events       JSONB NOT NULL DEFAULT '[]',
    secret       TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    description  TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wh_tenant
    ON webhook_configs(tenant_id);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id   TEXT PRIMARY KEY,
    webhook_id    TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    response_code INTEGER,
    response_body TEXT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_whd_webhook
    ON webhook_deliveries(webhook_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_whd_tenant
    ON webhook_deliveries(tenant_id, created_at DESC);
"""


# ============================================================================
# WebhookService
# ============================================================================

class WebhookService:
    """Manages webhook registration and event dispatch.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    """

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="webhook")

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        self._executor.shutdown(wait=False)
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Webhook schema ensured")

    # ------------------------------------------------------------------
    # CRUD — webhook configs
    # ------------------------------------------------------------------

    def register(
        self,
        tenant_id: str,
        url: str,
        events: list[str],
        description: str = "",
    ) -> WebhookConfig:
        """Register a new webhook endpoint.

        Parameters
        ----------
        tenant_id : str
        url : str
            The HTTPS URL to receive POST events.
        events : list[str]
            Event types to subscribe to.
        description : str
            Optional human-readable description.

        Returns
        -------
        WebhookConfig
            The created webhook configuration (includes the signing secret).
        """
        # Validate event types
        invalid = set(events) - VALID_EVENT_TYPES
        if invalid:
            raise ValueError(
                f"Invalid event types: {invalid}. "
                f"Valid: {sorted(VALID_EVENT_TYPES)}"
            )

        webhook_id = uuid.uuid4().hex[:24]
        secret = f"whsec_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO webhook_configs "
            "(webhook_id, tenant_id, url, events, secret, description, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (webhook_id, tenant_id, url, json.dumps(events), secret,
             description, now),
        )
        logger.info("Webhook registered: %s → %s (%s)", webhook_id, url, events)

        return WebhookConfig(
            webhook_id=webhook_id,
            tenant_id=tenant_id,
            url=url,
            events=events,
            secret=secret,
            description=description,
            created_at=now,
        )

    def list_webhooks(self, tenant_id: str) -> list[WebhookConfig]:
        """List all webhooks for a tenant."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM webhook_configs WHERE tenant_id = %s "
            "ORDER BY created_at DESC",
            (tenant_id,),
        ).fetchall()
        return [self._row_to_config(r) for r in rows]

    def get_webhook(self, webhook_id: str) -> WebhookConfig | None:
        """Get a single webhook config."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM webhook_configs WHERE webhook_id = %s",
            (webhook_id,),
        ).fetchone()
        return self._row_to_config(row) if row else None

    def update_webhook(
        self,
        webhook_id: str,
        tenant_id: str,
        url: str | None = None,
        events: list[str] | None = None,
        enabled: bool | None = None,
        description: str | None = None,
    ) -> WebhookConfig | None:
        """Update webhook fields."""
        updates = []
        params: list[Any] = []
        if url is not None:
            updates.append("url = %s")
            params.append(url)
        if events is not None:
            invalid = set(events) - VALID_EVENT_TYPES
            if invalid:
                raise ValueError(f"Invalid event types: {invalid}")
            updates.append("events = %s")
            params.append(json.dumps(events))
        if enabled is not None:
            updates.append("enabled = %s")
            params.append(enabled)
        if description is not None:
            updates.append("description = %s")
            params.append(description)

        if not updates:
            return self.get_webhook(webhook_id)

        params.extend([webhook_id, tenant_id])
        conn = self._get_conn()
        conn.execute(
            f"UPDATE webhook_configs SET {', '.join(updates)} "
            f"WHERE webhook_id = %s AND tenant_id = %s",
            params,
        )
        return self.get_webhook(webhook_id)

    def delete_webhook(self, webhook_id: str, tenant_id: str) -> bool:
        """Delete a webhook config."""
        conn = self._get_conn()
        result = conn.execute(
            "DELETE FROM webhook_configs "
            "WHERE webhook_id = %s AND tenant_id = %s",
            (webhook_id, tenant_id),
        )
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Delivery history
    # ------------------------------------------------------------------

    def get_deliveries(
        self,
        webhook_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[WebhookDelivery]:
        """Get delivery history for a webhook."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM webhook_deliveries "
            "WHERE webhook_id = %s ORDER BY created_at DESC "
            "LIMIT %s OFFSET %s",
            (webhook_id, limit, offset),
        ).fetchall()
        return [self._row_to_delivery(r) for r in rows]

    # ------------------------------------------------------------------
    # Dispatch — fire-and-forget with retry
    # ------------------------------------------------------------------

    def dispatch(
        self,
        tenant_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        """Dispatch a webhook event to all matching tenant webhooks.

        Delivery happens in a background thread pool — this method
        returns immediately.

        Parameters
        ----------
        tenant_id : str
        event_type : str
            One of the VALID_EVENT_TYPES.
        payload : dict
            Event data to send.

        Returns
        -------
        int
            Number of webhooks matched.
        """
        if event_type not in VALID_EVENT_TYPES:
            logger.warning("Unknown webhook event type: %s", event_type)
            return 0

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM webhook_configs "
            "WHERE tenant_id = %s AND enabled = TRUE",
            (tenant_id,),
        ).fetchall()

        matched = 0
        for row in rows:
            config = self._row_to_config(row)
            if event_type in config.events:
                delivery_id = uuid.uuid4().hex[:24]
                self._persist_delivery(delivery_id, config, event_type, payload)
                self._executor.submit(
                    self._deliver_with_retry,
                    delivery_id, config, event_type, payload,
                )
                matched += 1

        if matched:
            logger.info(
                "Dispatched %s to %d webhook(s) for tenant %s",
                event_type, matched, tenant_id,
            )
        return matched

    def _deliver_with_retry(
        self,
        delivery_id: str,
        config: WebhookConfig,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Deliver a webhook with exponential backoff retries."""
        import httpx

        body = json.dumps({
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        }, default=str)

        # HMAC-SHA256 signature
        signature = hmac.new(
            config.secret.encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-Grafomem-Event": event_type,
            "X-Grafomem-Signature": f"sha256={signature}",
            "X-Grafomem-Delivery": delivery_id,
            "User-Agent": "GRAFOMEM-Webhooks/1.0",
        }

        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = httpx.post(
                    config.url,
                    content=body,
                    headers=headers,
                    timeout=10.0,
                )
                self._update_delivery(
                    delivery_id,
                    status=DeliveryStatus.DELIVERED if resp.status_code < 400
                           else DeliveryStatus.RETRYING,
                    attempts=attempt + 1,
                    response_code=resp.status_code,
                    response_body=resp.text[:500],
                )
                if resp.status_code < 400:
                    logger.info(
                        "Webhook delivered: %s → %s (status=%d)",
                        delivery_id, config.url, resp.status_code,
                    )
                    try:
                        from aml.cloud.metrics import WEBHOOKS_DISPATCHED
                        WEBHOOKS_DISPATCHED.labels(event_type=event_type, success="true").inc()
                    except Exception:
                        pass
                    return

                logger.warning(
                    "Webhook delivery failed: %s → %s (status=%d, attempt %d/%d)",
                    delivery_id, config.url, resp.status_code,
                    attempt + 1, _MAX_ATTEMPTS,
                )

            except Exception as e:
                self._update_delivery(
                    delivery_id,
                    status=DeliveryStatus.RETRYING,
                    attempts=attempt + 1,
                    error=str(e),
                )
                logger.warning(
                    "Webhook delivery error: %s → %s (%s, attempt %d/%d)",
                    delivery_id, config.url, e, attempt + 1, _MAX_ATTEMPTS,
                )

            # Wait before retry (unless last attempt)
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])

        # All retries exhausted
        self._update_delivery(
            delivery_id,
            status=DeliveryStatus.FAILED,
            attempts=_MAX_ATTEMPTS,
        )
        logger.error(
            "Webhook delivery failed permanently: %s → %s",
            delivery_id, config.url,
        )
        try:
            from aml.cloud.metrics import WEBHOOKS_DISPATCHED
            WEBHOOKS_DISPATCHED.labels(event_type=event_type, success="false").inc()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Test delivery
    # ------------------------------------------------------------------

    def send_test(self, webhook_id: str, tenant_id: str) -> WebhookDelivery | None:
        """Send a test event to a webhook."""
        config = self.get_webhook(webhook_id)
        if config is None or config.tenant_id != tenant_id:
            return None

        delivery_id = uuid.uuid4().hex[:24]
        payload = {
            "test": True,
            "message": "This is a test webhook from GRAFOMEM Cloud.",
            "webhook_id": webhook_id,
        }
        self._persist_delivery(delivery_id, config, "test", payload)
        # Deliver synchronously for test
        self._deliver_with_retry(delivery_id, config, "test", payload)
        return self._get_delivery(delivery_id)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _persist_delivery(
        self,
        delivery_id: str,
        config: WebhookConfig,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Create a delivery record."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO webhook_deliveries "
                "(delivery_id, webhook_id, tenant_id, event_type, payload, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (delivery_id, config.webhook_id, config.tenant_id,
                 event_type, json.dumps(payload, default=str),
                 DeliveryStatus.PENDING.value),
            )
        except Exception as e:
            logger.warning("Failed to persist delivery record: %s", e)

    def _update_delivery(
        self,
        delivery_id: str,
        status: DeliveryStatus,
        attempts: int,
        response_code: int | None = None,
        response_body: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update delivery status."""
        try:
            conn = self._get_conn()
            completed = (
                datetime.now(timezone.utc)
                if status in (DeliveryStatus.DELIVERED, DeliveryStatus.FAILED)
                else None
            )
            conn.execute(
                "UPDATE webhook_deliveries SET "
                "status = %s, attempts = %s, response_code = %s, "
                "response_body = %s, error = %s, completed_at = %s "
                "WHERE delivery_id = %s",
                (status.value, attempts, response_code,
                 response_body, error, completed, delivery_id),
            )
        except Exception as e:
            logger.warning("Failed to update delivery: %s", e)

    def _get_delivery(self, delivery_id: str) -> WebhookDelivery | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM webhook_deliveries WHERE delivery_id = %s",
            (delivery_id,),
        ).fetchone()
        return self._row_to_delivery(row) if row else None

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_config(row: dict[str, Any]) -> WebhookConfig:
        events = row.get("events", [])
        if isinstance(events, str):
            events = json.loads(events)
        return WebhookConfig(
            webhook_id=row["webhook_id"],
            tenant_id=row["tenant_id"],
            url=row["url"],
            events=events,
            secret=row["secret"],
            enabled=row.get("enabled", True),
            description=row.get("description", ""),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_delivery(row: dict[str, Any]) -> WebhookDelivery:
        payload = row.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        return WebhookDelivery(
            delivery_id=row["delivery_id"],
            webhook_id=row["webhook_id"],
            tenant_id=row["tenant_id"],
            event_type=row["event_type"],
            payload=payload,
            status=DeliveryStatus(row["status"]),
            attempts=row.get("attempts", 0),
            response_code=row.get("response_code"),
            response_body=row.get("response_body"),
            error=row.get("error"),
            created_at=row["created_at"],
            completed_at=row.get("completed_at"),
        )

    @staticmethod
    def config_to_dict(c: WebhookConfig) -> dict[str, Any]:
        return {
            "webhook_id": c.webhook_id,
            "tenant_id": c.tenant_id,
            "url": c.url,
            "events": c.events,
            "enabled": c.enabled,
            "description": c.description,
            "created_at": c.created_at.isoformat(),
            # Secret shown only on creation
        }

    @staticmethod
    def delivery_to_dict(d: WebhookDelivery) -> dict[str, Any]:
        return {
            "delivery_id": d.delivery_id,
            "webhook_id": d.webhook_id,
            "event_type": d.event_type,
            "status": d.status.value,
            "attempts": d.attempts,
            "response_code": d.response_code,
            "error": d.error,
            "created_at": d.created_at.isoformat(),
            "completed_at": d.completed_at.isoformat() if d.completed_at else None,
        }
