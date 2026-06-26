"""
GRAFOMEM Stripe billing service — subscription checkout and lifecycle.

Manages Stripe Checkout sessions, webhook processing, and subscription
state persistence.  Backed by PostgreSQL via psycopg v3 (sync), matching
the same driver pattern as TenantManager and MeteringService.

Environment variables consumed:
  STRIPE_SECRET_KEY        – Stripe API secret key
  STRIPE_WEBHOOK_SECRET    – Stripe webhook signing secret
  STRIPE_STARTER_PRICE_ID  – Stripe Price ID for Starter plan
  STRIPE_PRO_PRICE_ID      – Stripe Price ID for Pro plan
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.billing")

# Stripe imported lazily so the module loads even without the SDK.
_stripe = None


def _get_stripe():
    """Lazily import and configure the stripe module."""
    global _stripe
    if _stripe is None:
        import stripe as _s
        _stripe = _s
    return _stripe


# ============================================================================
# Plan → Price mapping
# ============================================================================

def _price_ids() -> dict[str, str | None]:
    """Load Stripe Price IDs from environment."""
    return {
        "starter": os.environ.get("STRIPE_STARTER_PRICE_ID"),
        "pro": os.environ.get("STRIPE_PRO_PRICE_ID"),
        "enterprise": None,  # Custom / contact-us — no self-serve checkout
    }


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class SubscriptionInfo:
    """Snapshot of a tenant's Stripe subscription."""
    id: str
    tenant_id: str
    stripe_customer_id: str
    stripe_subscription_id: str | None
    plan: str
    status: str  # active, past_due, canceled, trialing
    current_period_end: datetime | None
    created_at: datetime


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS subscriptions (
    id                  TEXT        PRIMARY KEY,
    tenant_id           TEXT        NOT NULL UNIQUE,
    stripe_customer_id  TEXT        NOT NULL,
    stripe_subscription_id TEXT,
    plan                TEXT        NOT NULL DEFAULT 'starter',
    status              TEXT        NOT NULL DEFAULT 'active',
    current_period_end  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant
    ON subscriptions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_customer
    ON subscriptions (stripe_customer_id);
"""


# ============================================================================
# StripeBillingService
# ============================================================================

class StripeBillingService:
    """Manages Stripe Checkout, webhooks, and subscription persistence.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    stripe_secret_key : str | None
        Stripe API secret key.  Falls back to ``STRIPE_SECRET_KEY`` env var.
    webhook_secret : str | None
        Stripe webhook signing secret.  Falls back to
        ``STRIPE_WEBHOOK_SECRET`` env var.
    """

    def __init__(
        self,
        db_url: str,
        stripe_secret_key: str | None = None,
        webhook_secret: str | None = None,
        pool=None,
    ) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._pool = pool
        # Strip whitespace/newlines — env vars sometimes include trailing \n
        self._stripe_key = (stripe_secret_key or os.environ.get("STRIPE_SECRET_KEY") or "").strip() or None
        self._webhook_secret = (webhook_secret or os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip() or None

        if self._stripe_key:
            stripe = _get_stripe()
            stripe.api_key = self._stripe_key
            logger.info("Stripe billing configured")
        else:
            logger.warning("Stripe secret key not set — billing disabled")

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the ``subscriptions`` table if it does not exist."""
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Subscriptions schema ensured")

    # ------------------------------------------------------------------
    # Checkout
    # ------------------------------------------------------------------

    def create_checkout_session(
        self,
        tenant_id: str,
        plan: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """Create a Stripe Checkout Session and return the session URL.

        Parameters
        ----------
        tenant_id : str
            The GRAFOMEM tenant upgrading their plan.
        plan : str
            Target plan (``starter``, ``pro``).
        success_url, cancel_url : str
            Redirect URLs after checkout.

        Returns
        -------
        str
            The Checkout Session URL for redirect.

        Raises
        ------
        ValueError
            If the plan has no associated Stripe Price ID.
        """
        stripe = _get_stripe()
        price_id = _price_ids().get(plan)
        if not price_id:
            raise ValueError(
                f"No Stripe Price ID configured for plan {plan!r}. "
                "Set STRIPE_{PLAN}_PRICE_ID environment variable."
            )

        # Find or create Stripe customer for this tenant
        customer_id = self._get_or_create_customer(tenant_id)

        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"tenant_id": tenant_id, "plan": plan},
        )

        logger.info(
            "Checkout session created: tenant=%s plan=%s session=%s",
            tenant_id, plan, session.id,
        )
        return session.url

    def create_portal_session(self, tenant_id: str, return_url: str) -> str:
        """Create a Customer Portal session URL for managing subscriptions."""
        stripe = _get_stripe()
        if not stripe:
            raise RuntimeError("Stripe SDK not available")

        customer_id = self._get_or_create_customer(tenant_id)
        
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
        return session.url

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    def handle_webhook(self, payload: bytes, sig_header: str) -> dict:
        """Process a Stripe webhook event.

        Handles:
        - ``checkout.session.completed`` → activate subscription
        - ``invoice.payment_succeeded`` → renew / confirm
        - ``invoice.payment_failed`` → mark past_due
        - ``customer.subscription.deleted`` → mark canceled

        Returns
        -------
        dict
            ``{"status": "ok", "event_type": "..."}`` on success.
        """
        stripe = _get_stripe()

        if self._webhook_secret:
            event = stripe.Webhook.construct_event(
                payload, sig_header, self._webhook_secret,
            )
        else:
            import json
            event = stripe.Event.construct_from(
                json.loads(payload), stripe.api_key,
            )

        event_type = event["type"]
        data = event["data"]["object"]

        if event_type == "checkout.session.completed":
            self._on_checkout_completed(data)
        elif event_type == "invoice.payment_succeeded":
            self._on_payment_succeeded(data)
        elif event_type == "invoice.payment_failed":
            self._on_payment_failed(data)
        elif event_type == "customer.subscription.deleted":
            self._on_subscription_deleted(data)
        else:
            logger.debug("Unhandled Stripe event: %s", event_type)

        return {"status": "ok", "event_type": event_type}

    # ------------------------------------------------------------------
    # Subscription queries
    # ------------------------------------------------------------------

    def get_subscription(self, tenant_id: str) -> SubscriptionInfo | None:
        """Return the current subscription for a tenant, or ``None``."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE tenant_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return self._row_to_info(row) if row else None

    def cancel_subscription(self, tenant_id: str) -> bool:
        """Cancel a tenant's Stripe subscription.

        Returns ``True`` if successfully canceled.
        """
        sub = self.get_subscription(tenant_id)
        if not sub or not sub.stripe_subscription_id:
            return False

        stripe = _get_stripe()
        try:
            stripe.Subscription.cancel(sub.stripe_subscription_id)
        except Exception as e:
            logger.error("Failed to cancel subscription: %s", e)
            return False

        conn = self._get_conn()
        conn.execute(
            "UPDATE subscriptions SET status = 'canceled' WHERE tenant_id = %s",
            (tenant_id,),
        )
        logger.info("Subscription canceled for tenant %s", tenant_id)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_customer(self, tenant_id: str) -> str:
        """Find existing Stripe customer or create one."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT stripe_customer_id FROM subscriptions "
            "WHERE tenant_id = %s AND stripe_customer_id IS NOT NULL LIMIT 1",
            (tenant_id,),
        ).fetchone()

        if row and row["stripe_customer_id"]:
            return row["stripe_customer_id"]

        # Look up tenant email for the customer record
        email_row = conn.execute(
            "SELECT email, name FROM tenants WHERE id = %s", (tenant_id,),
        ).fetchone()

        stripe = _get_stripe()
        customer = stripe.Customer.create(
            metadata={"tenant_id": tenant_id},
            email=email_row["email"] if email_row else None,
            name=email_row["name"] if email_row else tenant_id,
        )
        return customer.id

    def _on_checkout_completed(self, session: dict) -> None:
        """Handle successful checkout — activate subscription."""
        tenant_id = session.get("metadata", {}).get("tenant_id")
        plan = session.get("metadata", {}).get("plan", "starter")
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")

        if not tenant_id:
            logger.warning("Checkout completed without tenant_id metadata")
            return

        record_id = uuid.uuid4().hex
        now = datetime.now(tz=timezone.utc)
        conn = self._get_conn()

        # Upsert subscription record
        conn.execute(
            "INSERT INTO subscriptions "
            "(id, tenant_id, stripe_customer_id, stripe_subscription_id, "
            " plan, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'active', %s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET "
            "  stripe_customer_id = EXCLUDED.stripe_customer_id, "
            "  stripe_subscription_id = EXCLUDED.stripe_subscription_id, "
            "  plan = EXCLUDED.plan, "
            "  status = 'active'",
            (record_id, tenant_id, customer_id, subscription_id, plan, now),
        )

        # Update tenant plan
        conn.execute(
            "UPDATE tenants SET plan = %s WHERE id = %s", (plan, tenant_id),
        )

        logger.info(
            "Subscription activated: tenant=%s plan=%s", tenant_id, plan,
        )

    def _on_payment_succeeded(self, invoice: dict) -> None:
        """Handle successful payment — update period end."""
        customer_id = invoice.get("customer")
        if not customer_id:
            return

        conn = self._get_conn()
        lines = invoice.get("lines", {}).get("data", [])
        period_end = None
        if lines:
            period_end_ts = lines[0].get("period", {}).get("end")
            if period_end_ts:
                period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc)

        conn.execute(
            "UPDATE subscriptions SET status = 'active', "
            "  current_period_end = %s "
            "WHERE stripe_customer_id = %s",
            (period_end, customer_id),
        )

    def _on_payment_failed(self, invoice: dict) -> None:
        """Handle failed payment — mark as past_due."""
        customer_id = invoice.get("customer")
        if not customer_id:
            return

        conn = self._get_conn()
        conn.execute(
            "UPDATE subscriptions SET status = 'past_due' "
            "WHERE stripe_customer_id = %s",
            (customer_id,),
        )
        logger.warning("Payment failed for customer %s", customer_id)

    def _on_subscription_deleted(self, subscription: dict) -> None:
        """Handle subscription cancellation — downgrade to starter."""
        customer_id = subscription.get("customer")
        if not customer_id:
            return

        conn = self._get_conn()
        # Find tenant and downgrade
        row = conn.execute(
            "SELECT tenant_id FROM subscriptions "
            "WHERE stripe_customer_id = %s LIMIT 1",
            (customer_id,),
        ).fetchone()

        if row:
            conn.execute(
                "UPDATE subscriptions SET status = 'canceled', plan = 'starter' "
                "WHERE stripe_customer_id = %s",
                (customer_id,),
            )
            conn.execute(
                "UPDATE tenants SET plan = 'starter' WHERE id = %s",
                (row["tenant_id"],),
            )
            logger.info("Subscription deleted, downgraded tenant %s", row["tenant_id"])

    @staticmethod
    def _row_to_info(row: dict[str, Any]) -> SubscriptionInfo:
        return SubscriptionInfo(
            id=row["id"],
            tenant_id=row["tenant_id"],
            stripe_customer_id=row["stripe_customer_id"],
            stripe_subscription_id=row.get("stripe_subscription_id"),
            plan=row["plan"],
            status=row["status"],
            current_period_end=row.get("current_period_end"),
            created_at=row["created_at"],
        )
