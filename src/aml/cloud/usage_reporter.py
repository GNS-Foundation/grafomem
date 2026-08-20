"""Cloud Metering Phase 3b — governed-decision → Stripe usage reporter (DARK by default).

Reports each tenant's period governed-decision count to a Stripe **sum-meter** as
idempotent *deltas* (``current − last_reported``), advancing a per-(tenant, period)
high-water-mark cursor ONLY on confirmed Stripe success. Two independent layers make
retries / double-invocation / out-of-order execution incapable of over-billing:

1. **Stripe-total guard** — ``report_base = max(cursor, stripe_meter_total)``. A delta
   that landed at Stripe but whose cursor write crashed is reflected in
   ``stripe_meter_total`` on the next tick, so it is never re-sent.
2. **High-water-mark idempotency key** — the ``identifier`` is keyed on the *target*
   count ``hash(tenant:period:current)``. Any retry that still sees the same ``current``
   emits the identical identifier, so Stripe deduplicates it exactly.

Reconciliation compares Stripe's aggregated total against the authoritative
``DecisionTrailService.get_usage``; drift is **surfaced (log/metric), never
auto-corrected upward** — the reporter never top-ups Stripe to cover a shortfall.

SHIP DARK: everything here is inert unless ``metered_enabled()`` is true, which requires
all three of ``STRIPE_METER_ID`` / ``STRIPE_BASE_PRICE_ID`` / ``STRIPE_OVERAGE_PRICE_ID``.
With those absent the reporter never starts, the cursor table is never created, the
checkout flow is byte-for-byte the legacy flat flow, and the existing webhooks are
untouched.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from aml.cloud.usage_routes import resolve_current_period

logger = logging.getLogger("grafomem.cloud.usage_reporter")

# Stripe imported lazily so the module loads without the SDK.
_stripe = None


def _get_stripe():
    global _stripe
    if _stripe is None:
        import stripe as _s
        _stripe = _s
    return _stripe


# ============================================================================
# Gate + config  (the three env vars are read ONLY here)
# ============================================================================

def _env(name: str) -> str | None:
    """Read an env var, stripping trailing whitespace/newlines (never trust raw)."""
    v = os.environ.get(name)
    v = v.strip() if v else v
    return v or None


def _is_duplicate_identifier(err: Exception) -> bool:
    """True when a MeterEvent emit failed *because* the identifier already exists.

    Stripe returns an ``InvalidRequestError`` whose message is
    ``"An event already exists with identifier <id>"``. Matching on the stable phrase
    keeps this independent of the SDK's error-class layout.
    """
    msg = str(getattr(err, "user_message", None) or err).lower()
    return "already exists" in msg and "identifier" in msg


def metered_enabled() -> bool:
    """True only when ALL of the metered-billing env vars are present.

    This is the single dark-launch gate: no meter id / base price / overage price ⇒
    the whole Phase-3b path stays inert.
    """
    return bool(_env("STRIPE_METER_ID") and _env("STRIPE_BASE_PRICE_ID") and _env("STRIPE_OVERAGE_PRICE_ID"))


def meter_config() -> dict[str, str] | None:
    """Return the metered-billing config, or ``None`` when not metered-enabled."""
    if not metered_enabled():
        return None
    return {
        "meter_id": _env("STRIPE_METER_ID"),
        "base_price_id": _env("STRIPE_BASE_PRICE_ID"),
        "overage_price_id": _env("STRIPE_OVERAGE_PRICE_ID"),
        # event_name identifies the meter's event stream for MeterEvent.create;
        # defaults to the 3a meter's event name.
        "event_name": _env("STRIPE_METER_EVENT_NAME") or "governed_decisions",
    }


# ============================================================================
# Schema (additive; created ONLY when metered-enabled)
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS usage_report_cursor (
    tenant_id       TEXT        NOT NULL,
    period_start    TIMESTAMPTZ NOT NULL,
    last_reported   BIGINT      NOT NULL DEFAULT 0,
    last_identifier TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, period_start)
);
"""

# Grace window: a Stripe meter summary lags aggregation by ~1 min. Only flag a
# "stripe_behind_cursor" (lost-event) drift once the cursor is older than this, so
# normal in-flight aggregation is never misreported as drift.
_RECONCILE_GRACE_SEC = 300


def _to_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ============================================================================
# UsageReporter
# ============================================================================

class UsageReporter:
    """Idempotent delta usage reporter for the Stripe sum-meter (Phase 3b, dark)."""

    def __init__(
        self,
        db_url: str,
        decision_trail,
        stripe_billing,
        *,
        tenant_manager=None,
        pool=None,
        interval_min: int = 15,
    ) -> None:
        self._db_url = db_url
        self._decision_trail = decision_trail
        self._stripe_billing = stripe_billing
        self._tenant_manager = tenant_manager
        self._pool = pool
        self._interval_min = interval_min
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    # ---- connection helpers ------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._db_url, row_factory=dict_row, autocommit=True)
        return self._conn

    def close(self) -> None:
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def ensure_schema(self) -> None:
        """Create the cursor table — caller must gate on ``metered_enabled()``."""
        self._get_conn().execute(_SCHEMA_SQL)
        logger.info("usage_report_cursor schema ensured")

    # ---- cursor read/write -------------------------------------------------

    def _read_cursor(self, tenant_id: str, period_start: datetime) -> tuple[int, str | None, datetime | None]:
        row = self._get_conn().execute(
            "SELECT last_reported, last_identifier, updated_at FROM usage_report_cursor "
            "WHERE tenant_id = %s AND period_start = %s",
            (tenant_id, period_start),
        ).fetchone()
        if not row:
            return 0, None, None
        return int(row["last_reported"]), row["last_identifier"], row["updated_at"]

    def _write_cursor(self, tenant_id: str, period_start: datetime, last_reported: int, identifier: str | None) -> None:
        self._get_conn().execute(
            "INSERT INTO usage_report_cursor (tenant_id, period_start, last_reported, last_identifier, updated_at) "
            "VALUES (%s, %s, %s, %s, now()) "
            "ON CONFLICT (tenant_id, period_start) DO UPDATE SET "
            "  last_reported = EXCLUDED.last_reported, "
            "  last_identifier = EXCLUDED.last_identifier, "
            "  updated_at = now()",
            (tenant_id, period_start, last_reported, identifier),
        )

    # ---- idempotency key ---------------------------------------------------

    @staticmethod
    def _identifier(tenant_id: str, period_start: datetime, current: int) -> str:
        """Deterministic MeterEvent identifier keyed on the TARGET high-water-mark.

        Two ticks that observe the same ``current`` produce the same identifier, so a
        retry of an already-landed report is deduplicated by Stripe. A grown ``current``
        yields a new identifier, but the Stripe-total guard keeps the delta correct.
        """
        raw = f"{tenant_id}:{int(period_start.timestamp())}:{current}"
        return "gm3b-" + hashlib.sha256(raw.encode()).hexdigest()[:40]

    # ---- reconcile ---------------------------------------------------------

    def _stripe_meter_total(self, customer_id: str, meter_id: str,
                            period_start: datetime, period_end: datetime) -> int | None:
        """Aggregated meter value for this customer over the period (hour-aligned window).

        Returns ``None`` on read failure (caller treats missing total as 0 for the
        guard but does NOT advance anything). Never raises.
        """
        stripe = _get_stripe()
        st = (int(period_start.timestamp()) // 3600) * 3600
        en = ((int(period_end.timestamp()) // 3600) + 1) * 3600
        try:
            summ = stripe.billing.Meter.list_event_summaries(
                meter_id, customer=customer_id, start_time=st, end_time=en,
            )
            total = 0.0
            for s in summ["data"]:
                if "aggregated_value" in s:
                    total += float(s["aggregated_value"])
            return int(total)
        except Exception as e:  # noqa: BLE001 — reconcile must never raise
            logger.warning("meter summary read failed (customer=%s): %s", customer_id, e)
            return None

    @staticmethod
    def _classify_drift(current: int, last_reported: int, stripe_total: int | None,
                        cursor_age_sec: float | None) -> dict | None:
        """Surface (never correct) meter/authoritative drift.

        * ``stripe_ahead`` — Stripe aggregated MORE than the authoritative count. An
          over-report already happened; the delta path will freeze (report_base>current).
        * ``stripe_behind_cursor`` — Stripe shows LESS than we recorded as reported, and
          the cursor is past the aggregation grace window ⇒ a reported event appears lost
          (under-billing). Surfaced for manual review; NEVER auto-topped-up.

        A Stripe total between ``last_reported`` and ``current`` is normal in-flight
        aggregation, not drift.
        """
        if stripe_total is None:
            return None
        if stripe_total > current:
            return {"type": "stripe_ahead", "stripe_total": stripe_total, "authoritative": current}
        if stripe_total < last_reported and (cursor_age_sec is None or cursor_age_sec > _RECONCILE_GRACE_SEC):
            return {"type": "stripe_behind_cursor", "stripe_total": stripe_total,
                    "last_reported": last_reported, "gap": last_reported - stripe_total}
        return None

    # ---- the core: report one tenant --------------------------------------

    def report_tenant(self, tenant_id: str, *, now: datetime | None = None) -> dict:
        """Report the current-period governed-decision delta for one tenant.

        Idempotent and over-bill-safe. Returns a structured result dict (never raises
        for expected skips; Stripe emit failures propagate so the cursor is NOT advanced).
        """
        cfg = meter_config()
        if cfg is None:
            return {"skipped": "not_metered"}

        sub = self._stripe_billing.get_subscription(tenant_id)
        if not sub or not sub.stripe_customer_id:
            return {"skipped": "no_customer", "tenant_id": tenant_id}
        customer_id = sub.stripe_customer_id

        start, end, source = resolve_current_period(
            tenant_id, _to_utc(sub.current_period_end) if sub.current_period_end else None, now=now,
        )

        usage = self._decision_trail.get_usage(tenant_id, start, end)
        current = int(usage["governed_decisions"])

        last_reported, _last_id, updated_at = self._read_cursor(tenant_id, start)
        stripe_total = self._stripe_meter_total(customer_id, cfg["meter_id"], start, end)

        cursor_age = None
        if updated_at is not None:
            cursor_age = (datetime.now(tz=timezone.utc) - _to_utc(updated_at)).total_seconds()
        drift = self._classify_drift(current, last_reported, stripe_total, cursor_age)
        if drift is not None:
            logger.warning("USAGE DRIFT tenant=%s %s", tenant_id, drift)

        # Guard: never re-send what Stripe already aggregated.
        report_base = max(last_reported, stripe_total if stripe_total is not None else 0)
        delta = current - report_base

        result = {
            "tenant_id": tenant_id, "period_start": start.isoformat(), "source": source,
            "current": current, "last_reported": last_reported,
            "stripe_total": stripe_total, "report_base": report_base, "drift": drift,
        }

        if delta <= 0:
            # Nothing to bill. Keep the cursor honest (advance to reflect a landed total),
            # but NEVER move it downward and NEVER emit.
            new_hwm = max(last_reported, report_base if stripe_total is not None else last_reported)
            if new_hwm > last_reported:
                self._write_cursor(tenant_id, start, new_hwm, _last_id)
            result["reported"] = 0
            return result

        identifier = self._identifier(tenant_id, start, current)
        stripe = _get_stripe()
        try:
            stripe.billing.MeterEvent.create(
                event_name=cfg["event_name"],
                identifier=identifier,
                payload={"stripe_customer_id": customer_id, "value": str(delta)},
            )
        except Exception as e:  # noqa: BLE001
            if _is_duplicate_identifier(e):
                # Stripe rejects a repeated identifier outright — which is PROOF the
                # event for this exact high-water-mark already landed. Treat as an
                # idempotent replay: advance the cursor, emit nothing more. This closes
                # the crash-after-emit gap even inside the meter-summary aggregation lag,
                # where the reconcile guard has not yet caught up. No double count possible.
                logger.info(
                    "meter event already exists (idempotent replay) tenant=%s id=%s — advancing cursor",
                    tenant_id, identifier,
                )
                self._write_cursor(tenant_id, start, current, identifier)
                result["reported"] = 0
                result["idempotent_replay"] = True
                result["identifier"] = identifier
                return result
            raise
        # Advance cursor ONLY after Stripe confirms the emit.
        self._write_cursor(tenant_id, start, current, identifier)
        result["reported"] = delta
        result["identifier"] = identifier
        return result

    # ---- batch + loop ------------------------------------------------------

    def _active_tenants(self) -> list[str]:
        """Tenants with an active subscription + customer (metered candidates)."""
        rows = self._get_conn().execute(
            "SELECT DISTINCT tenant_id FROM subscriptions "
            "WHERE stripe_customer_id IS NOT NULL AND status = 'active'",
        ).fetchall()
        return [r["tenant_id"] for r in rows]

    def run_once(self) -> list[dict]:
        """One reporting sweep over all active tenants. Per-tenant errors are isolated."""
        if not metered_enabled():
            return []
        out = []
        for tid in self._active_tenants():
            try:
                out.append(self.report_tenant(tid))
            except Exception as e:  # noqa: BLE001 — one tenant must not sink the sweep
                logger.error("usage report failed tenant=%s: %s", tid, e)
                out.append({"tenant_id": tid, "error": str(e)})
        return out

    async def start(self) -> None:
        """Start the periodic reporter — no-op unless metered-enabled."""
        if not metered_enabled():
            logger.info("usage reporter dark (metered_enabled=False) — not starting")
            return
        self.ensure_schema()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="usage-reporter")
        logger.info("usage reporter started (interval=%dm)", self._interval_min)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                await asyncio.sleep(self._interval_min * 60)
                if not self._running:
                    break
                await loop.run_in_executor(None, self.run_once)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error("usage reporter loop error: %s", e)
                await asyncio.sleep(60)
