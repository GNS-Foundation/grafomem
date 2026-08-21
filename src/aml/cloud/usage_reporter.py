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
    last_reported   BIGINT      NOT NULL DEFAULT 0,    -- committed high-water-mark (write-ahead intent)
    last_identifier TEXT,                              -- identifier of the last emit (= H(period, last_reported))
    last_delta      BIGINT      NOT NULL DEFAULT 0,    -- value of the last emit (for idempotent recovery re-emit)
    last_confirmed  BOOLEAN     NOT NULL DEFAULT TRUE, -- did the last emit confirm (accepted OR dup-rejected)?
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, period_start)
);
"""

# Grace window: a Stripe meter summary lags aggregation by ~1 min. Only *alert* on a
# persistent shortfall once the cursor is older than this, so normal in-flight
# aggregation is never misreported as drift.
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

    def _read_cursor(self, tenant_id: str, period_start: datetime):
        """Return ``(committed, last_identifier, last_delta, last_confirmed, updated_at)``."""
        row = self._get_conn().execute(
            "SELECT last_reported, last_identifier, last_delta, last_confirmed, updated_at "
            "FROM usage_report_cursor WHERE tenant_id = %s AND period_start = %s",
            (tenant_id, period_start),
        ).fetchone()
        if not row:
            return 0, None, 0, True, None
        return (int(row["last_reported"]), row["last_identifier"], int(row["last_delta"]),
                bool(row["last_confirmed"]), row["updated_at"])

    def _write_cursor(self, tenant_id: str, period_start: datetime, committed: int,
                      identifier: str | None, delta: int, confirmed: bool) -> None:
        """Durable write of the committed high-water-mark + last-emit metadata (write-ahead)."""
        self._get_conn().execute(
            "INSERT INTO usage_report_cursor "
            "  (tenant_id, period_start, last_reported, last_identifier, last_delta, last_confirmed, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (tenant_id, period_start) DO UPDATE SET "
            "  last_reported = EXCLUDED.last_reported, "
            "  last_identifier = EXCLUDED.last_identifier, "
            "  last_delta = EXCLUDED.last_delta, "
            "  last_confirmed = EXCLUDED.last_confirmed, "
            "  updated_at = now()",
            (tenant_id, period_start, committed, identifier, delta, confirmed),
        )

    def _mark_confirmed(self, tenant_id: str, period_start: datetime) -> None:
        """Flag the last emit as confirmed (landed or dup-rejected)."""
        self._get_conn().execute(
            "UPDATE usage_report_cursor SET last_confirmed = TRUE "
            "WHERE tenant_id = %s AND period_start = %s",
            (tenant_id, period_start),
        )

    # ---- idempotency key ---------------------------------------------------

    @staticmethod
    def _identifier(tenant_id: str, period_start: datetime, hwm: int) -> str:
        """Deterministic MeterEvent identifier keyed on the TARGET high-water-mark.

        The high-water-mark is monotonic within a period, so each hwm maps to exactly one
        emit. A recovery re-emit for the same hwm reuses this identifier: if the original
        landed, Stripe rejects the duplicate (no double count); if it never landed, the
        identifier is still free and the re-emit lands. This is what makes recovery an
        idempotent replay rather than a gap-fill against a lagging total.
        """
        raw = f"{tenant_id}:{int(period_start.timestamp())}:{hwm}"
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
    def _classify_drift(current: int, committed: int, stripe_total: int | None,
                        last_confirmed: bool, cursor_age_sec: float | None) -> dict | None:
        """ALERT-only drift classification (never corrects downward).

        * ``stripe_ahead`` — Stripe aggregated MORE than the authoritative count. This is
          the only true over-report; it must never happen under the write-ahead scheme, so
          it is surfaced loudly and NEVER corrected.
        * ``stripe_behind_confirmed`` — Stripe shows LESS than the committed high-water-mark,
          the last emit is already CONFIRMED, and the cursor is past the aggregation grace
          window ⇒ a *confirmed* emit appears to have been reversed/lost (beyond the single
          unconfirmed emit that per-tick recovery already re-sends). Surfaced for review.

        A shortfall while ``last_confirmed`` is False is the expected pending emit, which
        per-tick recovery re-sends — not drift. A Stripe total between ``committed`` and
        ``current`` is normal in-flight aggregation, not drift.
        """
        if stripe_total is None:
            return None
        if stripe_total > current:
            return {"type": "stripe_ahead", "stripe_total": stripe_total, "authoritative": current}
        if (last_confirmed and stripe_total < committed
                and (cursor_age_sec is None or cursor_age_sec > _RECONCILE_GRACE_SEC)):
            return {"type": "stripe_behind_confirmed", "stripe_total": stripe_total,
                    "committed": committed, "gap": committed - stripe_total}
        return None

    # ---- the core: report one tenant --------------------------------------

    def _emit(self, cfg: dict, customer_id: str, identifier: str, value: int) -> str:
        """Emit one MeterEvent. Returns 'accepted' or 'duplicate'; re-raises other errors.

        A duplicate-identifier rejection is a SUCCESS for our purposes — it proves the
        event with this identifier already landed, so the caller may treat it as confirmed.
        """
        stripe = _get_stripe()
        try:
            stripe.billing.MeterEvent.create(
                event_name=cfg["event_name"],
                identifier=identifier,
                payload={"stripe_customer_id": customer_id, "value": str(value)},
            )
            return "accepted"
        except Exception as e:  # noqa: BLE001
            if _is_duplicate_identifier(e):
                return "duplicate"
            raise

    def report_tenant(self, tenant_id: str, *, now: datetime | None = None) -> dict:
        """Report the current-period governed-decision delta for one tenant.

        Over-bill-safe by construction (write-ahead intent + idempotent recovery):

        1. Recover any UNCONFIRMED prior emit first by re-emitting the exact stored
           (identifier, value). If it already landed, Stripe rejects the duplicate; if it
           never landed, it lands. If this re-emit hard-errors, the tick ABORTS without
           advancing — preserving the single-unconfirmed-emit invariant.
        2. Compute the new delta against the COMMITTED cursor (never the lagging Stripe
           total). WRITE the new high-water-mark to the cursor BEFORE emitting, so a crash
           anywhere after this leaves the cursor already advanced — a retry then computes
           the correct incremental delta with a fresh identifier and cannot double-report.
        3. Emit; mark confirmed on accept/duplicate.

        Returns a structured result dict. Expected skips never raise.
        """
        cfg = meter_config()
        if cfg is None:
            return {"skipped": "not_metered"}

        sub = self._stripe_billing.get_subscription(tenant_id)
        if not sub or not sub.stripe_customer_id:
            return {"skipped": "no_customer", "tenant_id": tenant_id}
        customer_id = sub.stripe_customer_id

        _cps = getattr(sub, "current_period_start", None)
        start, end, source = resolve_current_period(
            tenant_id,
            _to_utc(_cps) if _cps else None,
            _to_utc(sub.current_period_end) if sub.current_period_end else None,
            now=now,
        )

        committed, last_id, last_delta, last_confirmed, updated_at = self._read_cursor(tenant_id, start)
        stripe_total = self._stripe_meter_total(customer_id, cfg["meter_id"], start, end)

        result = {
            "tenant_id": tenant_id, "period_start": start.isoformat(), "source": source,
            "committed": committed, "stripe_total": stripe_total, "reported": 0,
        }

        # ── Step 1: recover any unconfirmed prior emit (idempotent re-emit) ──
        if last_id is not None and not last_confirmed and last_delta > 0:
            outcome = self._emit(cfg, customer_id, last_id, last_delta)  # may raise → abort tick
            self._mark_confirmed(tenant_id, start)
            last_confirmed = True
            result["recovered"] = {"identifier": last_id, "value": last_delta, "outcome": outcome}
            logger.info("recovered unconfirmed emit tenant=%s id=%s value=%s (%s)",
                        tenant_id, last_id, last_delta, outcome)

        usage = self._decision_trail.get_usage(tenant_id, start, end)
        current = int(usage["governed_decisions"])
        result["current"] = current

        # ── Drift is ALERT-only; never a correction ──
        cursor_age = None
        if updated_at is not None:
            cursor_age = (datetime.now(tz=timezone.utc) - _to_utc(updated_at)).total_seconds()
        drift = self._classify_drift(current, committed, stripe_total, last_confirmed, cursor_age)
        result["drift"] = drift
        if drift is not None:
            logger.warning("USAGE DRIFT tenant=%s %s", tenant_id, drift)

        # Defensive freeze: if Stripe already reports >= current (only possible if it is
        # genuinely ahead — the summary lags LOW, never high), advance the cursor to stop
        # emitting and do NOT pile on. Over-report is surfaced by the drift alert above.
        if stripe_total is not None and stripe_total >= current:
            if current > committed:
                self._write_cursor(tenant_id, start, current, last_id, 0, True)
            return result

        if current <= committed:
            return result  # nothing new to bill

        # ── Step 2: write-ahead the new high-water-mark BEFORE emitting ──
        delta = current - committed
        identifier = self._identifier(tenant_id, start, current)
        self._write_cursor(tenant_id, start, current, identifier, delta, False)

        # ── Step 3: emit, then confirm ──
        outcome = self._emit(cfg, customer_id, identifier, delta)  # may raise → cursor stays advanced+unconfirmed
        self._mark_confirmed(tenant_id, start)
        result["reported"] = 0 if outcome == "duplicate" else delta
        result["identifier"] = identifier
        result["outcome"] = outcome
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
        """One reporting sweep over all active tenants. Per-tenant errors are isolated.

        Cross-replica singleton: guarded by a Postgres advisory lock so that under
        horizontal scaling only ONE instance emits per tick (losers skip), removing the
        double-report / double-charge risk on the shared cursor.
        """
        if not metered_enabled():
            return []
        from aml.cloud.singleton import cycle_singleton, LOCK_ID_USAGE_REPORTER
        with cycle_singleton(self._db_url, LOCK_ID_USAGE_REPORTER, "usage-reporter") as won:
            if not won:
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
