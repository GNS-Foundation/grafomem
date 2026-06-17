"""
GRAFOMEM Audit Logger — Immutable governance audit logs.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from aml.cloud.db_pool import RoutingPool

logger = logging.getLogger("grafomem.cloud.audit")


_SCHEMA_AUDIT = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_logs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_logs(timestamp);
"""


class AuditLogger:
    """Immutable audit logging backed by Postgres."""

    def __init__(self, db_pool: RoutingPool):
        self._db_pool = db_pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            with self._db_pool.connection() as conn:
                conn.execute(_SCHEMA_AUDIT)
        except Exception as e:
            logger.warning("Could not ensure audit_logs schema: %s", e)

    def log(
        self,
        tenant_id: str,
        actor: str,
        action: str,
        resource: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an immutable audit event."""
        import uuid
        
        event_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc)
        meta_json = json.dumps(metadata or {})
        
        try:
            with self._db_pool.connection() as conn:
                conn.execute(
                    """INSERT INTO audit_logs 
                       (id, tenant_id, actor, action, resource, metadata, timestamp) 
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (event_id, tenant_id, actor, action, resource, meta_json, ts),
                )
        except Exception as e:
            logger.error("Failed to write audit log: %s", e)

    def get_logs(
        self, tenant_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Retrieve audit logs for a tenant, ordered by timestamp descending."""
        try:
            with self._db_pool.connection(readonly=True) as conn:
                rows = conn.execute(
                    """SELECT id, tenant_id, actor, action, resource, metadata, timestamp 
                       FROM audit_logs 
                       WHERE tenant_id = %s 
                       ORDER BY timestamp DESC 
                       LIMIT %s""",
                    (tenant_id, limit),
                ).fetchall()

                return [
                    {
                        "id": row["id"],
                        "tenant_id": row["tenant_id"],
                        "actor": row["actor"],
                        "action": row["action"],
                        "resource": row["resource"],
                        "metadata": row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"]),
                        "timestamp": row["timestamp"].isoformat() if isinstance(row["timestamp"], datetime) else row["timestamp"]
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error("Failed to read audit logs: %s", e)
            return []
