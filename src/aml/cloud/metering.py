"""
GRAFOMEM metering service — per-tenant usage tracking for billing and limits.

Records every API operation (write, read, delete, supersede) with its byte
count and timestamp. Provides aggregated usage summaries and a sliding-window
rate limiter backed by PostgreSQL via psycopg v3 (sync).

The metering table is append-only and partitioned by time in production;
summaries are computed via SQL aggregation with no materialized views so the
schema stays migration-free for the MVP.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.metering")


# ============================================================================
# Core data types
# ============================================================================

VALID_OP_TYPES = frozenset({"write", "read", "delete", "supersede", "decision_log"})


@dataclass(slots=True)
class UsageSummary:
    """Aggregated usage for a tenant over a billing period."""
    tenant_id: str
    period: str
    writes: int = 0
    reads: int = 0
    deletes: int = 0
    supersedes: int = 0
    total_bytes: int = 0
    total_operations: int = 0


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS metering_events (
    id          TEXT        PRIMARY KEY,
    tenant_id   TEXT        NOT NULL,
    op_type     TEXT        NOT NULL,
    bytes_count INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metering_tenant_time
    ON metering_events (tenant_id, created_at DESC);
"""


# ============================================================================
# MeteringService
# ============================================================================

class MeteringService:
    """Tracks per-tenant API usage for billing and rate limiting.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    """

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Return an open connection, creating one lazily."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the ``metering_events`` table if it does not exist."""
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Metering schema ensured")

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_operation(
        self,
        tenant_id: str,
        op_type: str,
        bytes_count: int = 0,
    ) -> None:
        """Record a single API operation.

        Parameters
        ----------
        tenant_id : str
            The tenant performing the operation.
        op_type : str
            One of ``write``, ``read``, ``delete``, ``supersede``.
        bytes_count : int
            Size of the payload in bytes (content length for writes/reads).
        """
        if op_type not in VALID_OP_TYPES:
            raise ValueError(
                f"Unknown op_type {op_type!r}. "
                f"Valid types: {sorted(VALID_OP_TYPES)}"
            )

        event_id = uuid.uuid4().hex
        now = datetime.now(tz=timezone.utc)

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO metering_events (id, tenant_id, op_type, bytes_count, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (event_id, tenant_id, op_type, bytes_count, now),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_usage(
        self,
        tenant_id: str,
        period: str = "current_month",
    ) -> UsageSummary:
        """Return aggregated usage for a tenant over a billing period.

        Parameters
        ----------
        tenant_id : str
            The tenant to summarise.
        period : str
            ``"current_month"`` (default) or ``"previous_month"``.
            Determines the time window for the aggregation.

        Returns
        -------
        UsageSummary
        """
        if period == "current_month":
            time_clause = (
                "created_at >= date_trunc('month', now()) "
                "AND created_at < date_trunc('month', now()) + interval '1 month'"
            )
        elif period == "previous_month":
            time_clause = (
                "created_at >= date_trunc('month', now()) - interval '1 month' "
                "AND created_at < date_trunc('month', now())"
            )
        else:
            raise ValueError(
                f"Unknown period {period!r}. "
                "Supported: 'current_month', 'previous_month'"
            )

        conn = self._get_conn()
        row = conn.execute(
            f"SELECT "
            f"  COALESCE(SUM(CASE WHEN op_type = 'write'     THEN 1 ELSE 0 END), 0) AS writes, "
            f"  COALESCE(SUM(CASE WHEN op_type = 'read'      THEN 1 ELSE 0 END), 0) AS reads, "
            f"  COALESCE(SUM(CASE WHEN op_type = 'delete'    THEN 1 ELSE 0 END), 0) AS deletes, "
            f"  COALESCE(SUM(CASE WHEN op_type = 'supersede' THEN 1 ELSE 0 END), 0) AS supersedes, "
            f"  COALESCE(SUM(bytes_count), 0)                                        AS total_bytes, "
            f"  COUNT(*)                                                              AS total_operations "
            f"FROM metering_events "
            f"WHERE tenant_id = %s AND {time_clause}",
            (tenant_id,),
        ).fetchone()

        if row is None:
            return UsageSummary(tenant_id=tenant_id, period=period)

        return UsageSummary(
            tenant_id=tenant_id,
            period=period,
            writes=row["writes"],
            reads=row["reads"],
            deletes=row["deletes"],
            supersedes=row["supersedes"],
            total_bytes=row["total_bytes"],
            total_operations=row["total_operations"],
        )

    def check_rate_limit(
        self,
        tenant_id: str,
        limits: Any,  # TenantLimits — avoid circular import
    ) -> bool:
        """Check whether a tenant is within their per-minute rate limit.

        Uses a 60-second sliding window over ``metering_events``.

        Parameters
        ----------
        tenant_id : str
            The tenant to check.
        limits : TenantLimits
            The tenant's plan limits (``max_requests_per_minute``).

        Returns
        -------
        bool
            ``True`` if the tenant is within limits and may proceed.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt "
            "FROM metering_events "
            "WHERE tenant_id = %s "
            "  AND created_at >= now() - interval '1 minute'",
            (tenant_id,),
        ).fetchone()

        count = row["cnt"] if row else 0
        return count < limits.max_requests_per_minute
