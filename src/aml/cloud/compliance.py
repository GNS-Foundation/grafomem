"""
GRAFOMEM compliance tracker — GMP conformance audit trail.

Records per-tenant, per-store conformance results (conformance rate,
declared capabilities, optional JSON report) and exposes latest/history
queries.  Backed by PostgreSQL via psycopg v3 (sync).

The compliance endpoints let operators answer "does tenant X's store Y
still pass the GMP conformance suite?" — the single most important
question for a production memory platform.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.compliance")


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class AuditRecord:
    """A single conformance audit result for one tenant/store pair."""
    id: str
    tenant_id: str
    store_id: str
    conformance_rate: float
    capabilities: list[str]
    audited_at: datetime
    report_json: str | None = None


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS compliance_audits (
    id                TEXT        PRIMARY KEY,
    tenant_id         TEXT        NOT NULL,
    store_id          TEXT        NOT NULL,
    conformance_rate  DOUBLE PRECISION NOT NULL,
    capabilities      TEXT[]      NOT NULL DEFAULT '{}',
    audited_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    report_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_compliance_tenant
    ON compliance_audits (tenant_id, audited_at DESC);
"""


# ============================================================================
# ComplianceTracker
# ============================================================================

class ComplianceTracker:
    """Tracks GMP conformance status for each tenant's stores.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    """

    def __init__(self, db_url: str, pool=None) -> None:
        self._db_url = db_url
        self._pool = pool
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Return an open connection, creating one lazily."""
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the ``compliance_audits`` table if it does not exist."""
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Compliance schema ensured")

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_audit(
        self,
        tenant_id: str,
        store_id: str,
        conformance_rate: float,
        capabilities: list[str],
        report_json: str | None = None,
    ) -> AuditRecord:
        """Persist a conformance audit result.

        Parameters
        ----------
        tenant_id : str
            The tenant that was audited.
        store_id : str
            The store within that tenant.
        conformance_rate : float
            Fraction of conformance checks that passed (0.0 – 1.0).
        capabilities : list[str]
            Capability flags declared by the store at audit time.
        report_json : str, optional
            Full JSON conformance report for deep-dive inspection.

        Returns
        -------
        AuditRecord
        """
        record_id = uuid.uuid4().hex
        now = datetime.now(tz=timezone.utc)

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO compliance_audits "
            "(id, tenant_id, store_id, conformance_rate, capabilities, "
            " audited_at, report_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (record_id, tenant_id, store_id, conformance_rate,
             capabilities, now, report_json),
        )
        logger.info(
            "Audit recorded: tenant=%s store=%s rate=%.2f",
            tenant_id, store_id, conformance_rate,
        )

        return AuditRecord(
            id=record_id,
            tenant_id=tenant_id,
            store_id=store_id,
            conformance_rate=conformance_rate,
            capabilities=capabilities,
            audited_at=now,
            report_json=report_json,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_latest(self, tenant_id: str) -> AuditRecord | None:
        """Return the most recent audit record for a tenant, or ``None``."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, tenant_id, store_id, conformance_rate, capabilities, "
            "       audited_at, report_json "
            "FROM compliance_audits "
            "WHERE tenant_id = %s "
            "ORDER BY audited_at DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_history(
        self, tenant_id: str, limit: int = 10,
    ) -> list[AuditRecord]:
        """Return the *limit* most recent audit records for a tenant."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, tenant_id, store_id, conformance_rate, capabilities, "
            "       audited_at, report_json "
            "FROM compliance_audits "
            "WHERE tenant_id = %s "
            "ORDER BY audited_at DESC LIMIT %s",
            (tenant_id, limit),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_all_latest(self) -> list[AuditRecord]:
        """Return the latest audit record per tenant (global dashboard).

        Uses ``DISTINCT ON`` to efficiently pick the newest row per tenant.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT ON (tenant_id) "
            "       id, tenant_id, store_id, conformance_rate, capabilities, "
            "       audited_at, report_json "
            "FROM compliance_audits "
            "ORDER BY tenant_id, audited_at DESC",
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> AuditRecord:
        """Convert a database row dict into an :class:`AuditRecord`."""
        return AuditRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            store_id=row["store_id"],
            conformance_rate=row["conformance_rate"],
            capabilities=list(row["capabilities"] or []),
            audited_at=row["audited_at"],
            report_json=row.get("report_json"),
        )
