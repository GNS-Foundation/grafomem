"""Cloud Metering Phase 3c — Free (starter) hard ceiling (BLOCK-CAPABLE, DARK by default).

Blocks *new governed decisions* (`cgr.decision.v1`, the metered unit) for free-tier
(`starter`) tenants once they pass ``FREE_CEILING`` in the current period. Everything here
is inert unless ``FREE_CEILING_ENABLED`` is set, and FAIL-OPEN everywhere: any miss, stale
row, DB error, unknown/NULL plan, or exception → the decision is ALLOWED.

Design (per review):
- The background refresh writes a ``free_usage_cache`` row ONLY for current ``starter``
  tenants. So the per-decision hot path is a single PK cache read:
    flag off / no row / row stale / count < ceiling  → ALLOW  (this is EVERY pro/enterprise
    decision — they never have a row, so no ``get_tenant`` call is made).
- ONLY when a fresh row shows ``count >= ceiling`` do we do an authoritative
  ``get_tenant`` re-check and block **only if the plan is still ``starter``**. This is what
  makes a stale ``starter`` row for a now-``pro`` tenant unable to block a paying customer.
- The whole check is wrapped in try/except → ALLOW on any error.

The check is enforced at the ``DecisionTrailService.log()`` choke point (gated on the
``cgr.decision.v1`` marker), so it covers the API, the orchestrator, and the demo router
in one place — never outcomes/reviews/reads/retrieval.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from aml.cloud.plan_config import (
    FREE_CEILING,
    FREE_CEILING_REFRESH_MIN,
    FREE_CEILING_STALE_SEC,
    FREE_PLAN,
)

logger = logging.getLogger("grafomem.cloud.free_ceiling")

CGR_DECISION_SCHEMA = "cgr.decision.v1"


class FreeCeilingExceeded(Exception):
    """Raised when a free-tier tenant is over its governed-decision ceiling.

    Carries an upgrade-oriented message; translated to HTTP 402 by the app's handler.
    """

    def __init__(self, tenant_id: str, count: int, ceiling: int) -> None:
        self.tenant_id = tenant_id
        self.count = count
        self.ceiling = ceiling
        super().__init__(
            f"Free plan governed-decision ceiling reached ({count} ≥ {ceiling} this period). "
            f"Upgrade to Pro to keep making governed decisions."
        )


def free_ceiling_enabled() -> bool:
    """The dark-launch gate. Off unless FREE_CEILING_ENABLED is a truthy env value."""
    v = (os.environ.get("FREE_CEILING_ENABLED") or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS free_usage_cache (
    tenant_id      TEXT        NOT NULL,
    period_start   TIMESTAMPTZ NOT NULL,
    governed_count BIGINT      NOT NULL DEFAULT 0,
    plan_hint      TEXT,
    refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, period_start)
);
"""


def _calendar_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The UTC calendar month containing ``now`` — the window for free/no-sub tenants.

    Free (starter) tenants have no paid subscription, so their period is always the
    calendar month (matches ``resolve_current_period``'s calendar branch). Computing this
    locally keeps the hot path DB-free.
    """
    now = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 \
        else start.replace(month=start.month + 1)
    return start, end


class FreeCeilingService:
    """Free-tier ceiling enforcement + the background cache refresh (Phase 3c, dark)."""

    def __init__(
        self,
        db_url: str,
        decision_trail,
        tenant_manager,
        *,
        pool=None,
        ceiling: int = FREE_CEILING,
        refresh_min: int = FREE_CEILING_REFRESH_MIN,
        stale_sec: int = FREE_CEILING_STALE_SEC,
    ) -> None:
        self._db_url = db_url
        self._decision_trail = decision_trail
        self._tenant_manager = tenant_manager
        self._pool = pool
        self._ceiling = ceiling
        self._refresh_min = refresh_min
        self._stale_sec = stale_sec
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    # ---- connection helpers ----------------------------------------------

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
        """Create free_usage_cache — caller must gate on ``free_ceiling_enabled()``."""
        self._get_conn().execute(_SCHEMA_SQL)
        logger.info("free_usage_cache schema ensured")

    # ---- hot path: the per-decision check --------------------------------

    def check(self, tenant_id: str, *, now: datetime | None = None) -> None:
        """Raise ``FreeCeilingExceeded`` iff this free-tier tenant is over the ceiling.

        FAIL-OPEN: returns (allows) on flag-off, cache miss, stale row, under-ceiling, DB
        error, unknown/NULL plan, or ANY exception. Only a fresh ``count >= ceiling`` row
        followed by an authoritative ``plan == FREE_PLAN`` re-check results in a block.
        """
        try:
            if not free_ceiling_enabled():
                return
            start, _end = _calendar_period(now)
            row = self._get_conn().execute(
                "SELECT governed_count, refreshed_at FROM free_usage_cache "
                "WHERE tenant_id = %s AND period_start = %s",
                (tenant_id, start),
            ).fetchone()
            if not row:
                return  # not a cached starter tenant (every pro/enterprise decision lands here)
            refreshed_at = row["refreshed_at"]
            if refreshed_at is not None:
                age = (datetime.now(tz=timezone.utc) - _to_utc(refreshed_at)).total_seconds()
                if age > self._stale_sec:
                    logger.warning("free ceiling: stale cache row tenant=%s age=%.0fs → allow",
                                   tenant_id, age)
                    return  # stale → never block
            count = int(row["governed_count"])
            if count < self._ceiling:
                return  # under ceiling → allow

            # Boundary only: authoritative plan re-check. A stale/hint 'starter' row for a
            # now-pro tenant CANNOT block them — the block requires plan == FREE_PLAN here.
            plan = self._authoritative_plan(tenant_id)
            if plan != FREE_PLAN:
                logger.info("free ceiling: tenant=%s over cached ceiling but plan=%r → allow",
                            tenant_id, plan)
                return
            logger.info("free ceiling BLOCK tenant=%s count=%s ceiling=%s",
                        tenant_id, count, self._ceiling)
            raise FreeCeilingExceeded(tenant_id, count, self._ceiling)
        except FreeCeilingExceeded:
            raise
        except Exception as e:  # noqa: BLE001 — enforcement must never break the decision path
            logger.warning("free ceiling check errored (fail-open, allow) tenant=%s: %s", tenant_id, e)
            return

    def _authoritative_plan(self, tenant_id: str) -> str | None:
        try:
            info = self._tenant_manager.get_tenant(tenant_id) if self._tenant_manager else None
            return info.plan if info is not None else None
        except Exception as e:  # noqa: BLE001
            logger.warning("free ceiling: get_tenant failed tenant=%s (fail-open): %s", tenant_id, e)
            return None  # unknown → not FREE_PLAN → allow

    # ---- background refresh ----------------------------------------------

    def refresh_once(self) -> int:
        """Recompute free_usage_cache for CURRENT starter tenants. Returns rows written."""
        if not free_ceiling_enabled():
            return 0
        start, end = _calendar_period()
        conn = self._get_conn()
        try:
            tenants = conn.execute(
                "SELECT id FROM tenants WHERE plan = %s", (FREE_PLAN,),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("free ceiling refresh: tenant scan failed: %s", e)
            return 0
        written = 0
        for t in tenants:
            tid = t["id"]
            try:
                usage = self._decision_trail.get_usage(tid, start, end)
                count = int(usage["governed_decisions"])
                conn.execute(
                    "INSERT INTO free_usage_cache "
                    "  (tenant_id, period_start, governed_count, plan_hint, refreshed_at) "
                    "VALUES (%s, %s, %s, %s, now()) "
                    "ON CONFLICT (tenant_id, period_start) DO UPDATE SET "
                    "  governed_count = EXCLUDED.governed_count, "
                    "  plan_hint = EXCLUDED.plan_hint, "
                    "  refreshed_at = now()",
                    (tid, start, count, FREE_PLAN),
                )
                written += 1
            except Exception as e:  # noqa: BLE001 — one tenant must not sink the sweep
                logger.warning("free ceiling refresh failed tenant=%s: %s", tid, e)
        logger.info("free ceiling refresh: %d starter tenant row(s) updated", written)
        return written

    async def start(self) -> None:
        """Start the periodic refresh — no-op unless enabled."""
        if not free_ceiling_enabled():
            logger.info("free ceiling dark (FREE_CEILING_ENABLED off) — not starting")
            return
        self.ensure_schema()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="free-ceiling-refresh")
        logger.info("free ceiling refresh started (interval=%dm, ceiling=%d)",
                    self._refresh_min, self._ceiling)

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
        # prime once at startup so the cache is warm before the first interval elapses
        try:
            await loop.run_in_executor(None, self.refresh_once)
        except Exception as e:  # noqa: BLE001
            logger.warning("free ceiling initial refresh error: %s", e)
        while self._running:
            try:
                await asyncio.sleep(self._refresh_min * 60)
                if not self._running:
                    break
                await loop.run_in_executor(None, self.refresh_once)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error("free ceiling refresh loop error: %s", e)
                await asyncio.sleep(60)


def _to_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
