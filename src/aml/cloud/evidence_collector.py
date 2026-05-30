"""
GRAFOMEM Evidence Collector — append-only audit service for governance.

Owns the ``governance_evaluation_log`` table. Persists evaluation verdicts,
provides query/export capabilities, and cross-references logs to Decision
Trail records for end-to-end traceability.

Extracted from GovernanceGateway to separate evidence generation from
policy enforcement (PEP) and policy evaluation (PDP).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from aml.cloud.governance import EvaluationLog, EvaluationResult
from aml.cloud.policy_engine import Verdict

logger = logging.getLogger("grafomem.cloud.evidence_collector")


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS governance_evaluation_log (
    log_id          TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    policy_id       TEXT NOT NULL,
    policy_name     TEXT NOT NULL,
    result          TEXT NOT NULL,
    operation       TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    request_summary TEXT NOT NULL DEFAULT '',
    decision_id     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gel_tenant
    ON governance_evaluation_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gel_policy
    ON governance_evaluation_log(policy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gel_decision
    ON governance_evaluation_log(decision_id)
    WHERE decision_id IS NOT NULL;
"""

# Migration: add decision_id column if table already exists without it
_MIGRATION_SQL = """\
ALTER TABLE governance_evaluation_log
    ADD COLUMN IF NOT EXISTS decision_id TEXT;
"""


# ============================================================================
# EvidenceCollector
# ============================================================================

class EvidenceCollector:
    """Append-only audit service for governance evaluation evidence.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    """

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        # First ensure migration column exists (may already exist from
        # governance.py creating the table without decision_id)
        try:
            conn.execute(_MIGRATION_SQL)
        except Exception:
            pass  # Column may not exist yet if table doesn't exist
        # Now create table + indexes (all IF NOT EXISTS, safe to re-run)
        conn.execute(_SCHEMA_SQL)
        logger.info("Evidence Collector schema ensured")

    # ------------------------------------------------------------------
    # Log evaluation
    # ------------------------------------------------------------------

    def log_evaluation(
        self,
        tenant_id: str,
        verdict: Verdict,
        operation: str,
        context: dict[str, Any],
        decision_id: str | None = None,
    ) -> EvaluationLog:
        """Persist a single policy evaluation verdict.

        Parameters
        ----------
        tenant_id : str
            Tenant that triggered the evaluation.
        verdict : Verdict
            The policy engine's verdict.
        operation : str
            Operation type (e.g. "write", "inference").
        context : dict
            Request context for summary generation.
        decision_id : str, optional
            Link to a Decision Trail record.

        Returns
        -------
        EvaluationLog
            The persisted log entry.
        """
        log_id = uuid.uuid4().hex[:24]
        now = datetime.now(tz=timezone.utc)
        summary = self._summarize_request(operation, context)

        log_entry = EvaluationLog(
            log_id=log_id,
            tenant_id=tenant_id,
            policy_id=verdict.policy_id,
            policy_name=verdict.policy_name,
            result=verdict.result,
            operation=operation,
            detail=verdict.detail,
            request_summary=summary,
            created_at=now,
        )

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO governance_evaluation_log "
            "(log_id, tenant_id, policy_id, policy_name, result, "
            " operation, detail, request_summary, decision_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                log_id, tenant_id, verdict.policy_id, verdict.policy_name,
                verdict.result.value, operation, verdict.detail,
                summary, decision_id, now,
            ),
        )

        return log_entry

    def log_verdicts(
        self,
        tenant_id: str,
        verdicts: list[Verdict],
        operation: str,
        context: dict[str, Any],
        decision_id: str | None = None,
    ) -> list[EvaluationLog]:
        """Persist multiple verdicts in one call."""
        return [
            self.log_evaluation(tenant_id, v, operation, context, decision_id)
            for v in verdicts
        ]

    # ------------------------------------------------------------------
    # Link to Decision Trail
    # ------------------------------------------------------------------

    def link_to_decision(self, log_id: str, decision_id: str) -> None:
        """Cross-reference an evaluation log to a Decision Trail record."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE governance_evaluation_log SET decision_id = %s "
            "WHERE log_id = %s",
            (decision_id, log_id),
        )

    # ------------------------------------------------------------------
    # Query logs
    # ------------------------------------------------------------------

    def get_logs(
        self,
        tenant_id: str,
        policy_id: str | None = None,
        result: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EvaluationLog]:
        conn = self._get_conn()
        conditions = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]

        if policy_id:
            conditions.append("policy_id = %s")
            params.append(policy_id)
        if result:
            conditions.append("result = %s")
            params.append(result)

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        rows = conn.execute(
            f"SELECT * FROM governance_evaluation_log "
            f"WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params,
        ).fetchall()

        return [self._row_to_log(r) for r in rows]

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS total_evals, "
            "  COUNT(CASE WHEN result = 'denied' THEN 1 END) AS denied, "
            "  COUNT(CASE WHEN result = 'escalated' THEN 1 END) AS escalated, "
            "  COUNT(CASE WHEN result = 'logged' THEN 1 END) AS logged, "
            "  COUNT(CASE WHEN result = 'allowed' THEN 1 END) AS allowed "
            "FROM governance_evaluation_log WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        return {
            "evaluations_total": row["total_evals"] if row else 0,
            "evaluations_denied": row["denied"] if row else 0,
            "evaluations_escalated": row["escalated"] if row else 0,
            "evaluations_logged": row["logged"] if row else 0,
            "evaluations_allowed": row["allowed"] if row else 0,
        }

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_evidence(
        self,
        tenant_id: str,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Streaming export of evaluation evidence for compliance reporting."""
        conn = self._get_conn()
        conditions = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]

        if from_time:
            conditions.append("created_at >= %s")
            params.append(from_time)
        if to_time:
            conditions.append("created_at <= %s")
            params.append(to_time)

        where = " AND ".join(conditions)
        cursor = conn.execute(
            f"SELECT * FROM governance_evaluation_log "
            f"WHERE {where} ORDER BY created_at ASC",
            params,
        )

        for row in cursor:
            yield self.log_to_dict(self._row_to_log(row))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize_request(operation: str, context: dict) -> str:
        parts = [f"op={operation}"]
        if context.get("model_id"):
            parts.append(f"model={context['model_id']}")
        if context.get("store_id"):
            parts.append(f"store={context['store_id']}")
        if context.get("query"):
            q = context["query"]
            parts.append(f"query=\"{q[:50]}{'…' if len(q) > 50 else ''}\"")
        return " ".join(parts)

    @staticmethod
    def _row_to_log(row: dict[str, Any]) -> EvaluationLog:
        return EvaluationLog(
            log_id=row["log_id"],
            tenant_id=row["tenant_id"],
            policy_id=row["policy_id"],
            policy_name=row["policy_name"],
            result=EvaluationResult(row["result"]),
            operation=row["operation"],
            detail=row["detail"],
            request_summary=row.get("request_summary", ""),
            created_at=row["created_at"],
        )

    @staticmethod
    def log_to_dict(log: EvaluationLog) -> dict[str, Any]:
        return {
            "log_id": log.log_id,
            "tenant_id": log.tenant_id,
            "policy_id": log.policy_id,
            "policy_name": log.policy_name,
            "result": log.result.value,
            "operation": log.operation,
            "detail": log.detail,
            "request_summary": log.request_summary,
            "created_at": log.created_at.isoformat(),
        }
