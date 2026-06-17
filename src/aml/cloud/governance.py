"""
GRAFOMEM Governance Gateway — policy-as-code for AI agent behavior.

A pre-execution policy engine that evaluates rules BEFORE an agent
operation is permitted. Every request passes through the gateway, which
checks it against the tenant's active policies and either allows,
denies, or escalates to a human-in-the-loop (HITL) gate.

Policy types:
  - rate_limit: max operations per time window
  - model_allowlist: restrict which LLM models can be used
  - content_filter: block queries or outputs matching patterns
  - data_scope: restrict which stores or tenants can be accessed
  - token_budget: cap total tokens per period
  - hitl_required: require human approval for specified operations
  - pii_guard: block PII patterns in outputs

Backed by PostgreSQL via psycopg v3 (sync), following the same patterns
as ComplianceTracker, DecisionTrailService, and ErasureProofService.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.governance")


# ============================================================================
# Enumerations
# ============================================================================

class PolicyType(str, Enum):
    RATE_LIMIT = "rate_limit"
    MODEL_ALLOWLIST = "model_allowlist"
    CONTENT_FILTER = "content_filter"
    DATA_SCOPE = "data_scope"
    TOKEN_BUDGET = "token_budget"
    HITL_REQUIRED = "hitl_required"
    PII_GUARD = "pii_guard"
    WORLD_MODEL_CONSTRAINT = "world_model_constraint"
    TOOL_DENY = "tool_deny"


class PolicyAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"  # Human-in-the-loop
    LOG_ONLY = "log_only"  # Allow but log a warning


class EvaluationResult(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    ESCALATED = "escalated"
    LOGGED = "logged"


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class Policy:
    """A governance policy definition."""
    policy_id: str
    tenant_id: str
    name: str
    description: str
    policy_type: PolicyType
    action: PolicyAction
    config: dict[str, Any]  # Type-specific configuration
    enabled: bool = True
    priority: int = 100  # Lower = higher priority
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class EvaluationLog:
    """Record of a policy evaluation."""
    log_id: str
    tenant_id: str
    policy_id: str
    policy_name: str
    result: EvaluationResult
    operation: str  # e.g. "write", "retrieve", "inference"
    detail: str
    request_summary: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================================================
# Default policies
# ============================================================================

DEFAULT_POLICIES = [
    {
        "name": "Default Rate Limit",
        "description": "Max 600 requests per minute (Pro tier default)",
        "policy_type": PolicyType.RATE_LIMIT,
        "action": PolicyAction.DENY,
        "config": {"max_requests": 600, "window_seconds": 60},
        "priority": 10,
    },
    {
        "name": "PII Output Guard",
        "description": "Detect and flag PII patterns in model outputs",
        "policy_type": PolicyType.PII_GUARD,
        "action": PolicyAction.LOG_ONLY,
        "config": {
            "patterns": [
                r"\b\d{3}-\d{2}-\d{4}\b",          # SSN
                r"\b\d{16}\b",                       # Credit card
                r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]{0,16})?\b",  # IBAN
            ],
            "description": "SSN, credit card, IBAN patterns"
        },
        "priority": 20,
    },
]


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS governance_policies (
    policy_id       TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    policy_type     TEXT NOT NULL,
    action          TEXT NOT NULL DEFAULT 'deny',
    config          JSONB NOT NULL DEFAULT '{}',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gp_tenant
    ON governance_policies(tenant_id, enabled, priority);

CREATE TABLE IF NOT EXISTS governance_evaluation_log (
    log_id          TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    policy_id       TEXT NOT NULL,
    policy_name     TEXT NOT NULL,
    result          TEXT NOT NULL,
    operation       TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    request_summary TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gel_tenant
    ON governance_evaluation_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gel_policy
    ON governance_evaluation_log(policy_id, created_at DESC);
"""


# ============================================================================
# GovernanceGateway
# ============================================================================

class GovernanceGateway:
    """Policy enforcement gateway (PEP) for constraining agent behavior.

    Delegates policy evaluation to PolicyEngine (PDP) and evidence
    logging to EvidenceCollector.  All public APIs remain unchanged.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    policy_engine : PolicyEngine, optional
        Custom engine instance.  Created automatically if not provided.
    evidence_collector : EvidenceCollector, optional
        Custom collector instance.  Created automatically if not provided.
    """

    def __init__(
        self,
        db_url: str,
        policy_engine: Any | None = None,
        evidence_collector: Any | None = None,
        pool=None,
    ) -> None:
        self._db_url = db_url
        self._pool = pool
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        # In-memory rate limit counters kept for backward compat
        self._rate_counters: dict[tuple[str, str], list[float]] = {}

        # PDP / Evidence delegation
        from aml.cloud.policy_engine import PolicyEngine as _PE
        from aml.cloud.evidence_collector import EvidenceCollector as _EC
        self._engine: _PE = policy_engine or _PE()
        self._evidence: _EC = evidence_collector or _EC(db_url)

    # ------------------------------------------------------------------
    # Connection
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
        elif self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._evidence.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        self._evidence.ensure_schema()
        logger.info("Governance Gateway schema ensured")

    # ------------------------------------------------------------------
    # Policy CRUD
    # ------------------------------------------------------------------

    def create_policy(
        self,
        tenant_id: str,
        name: str,
        description: str,
        policy_type: PolicyType | str,
        action: PolicyAction | str,
        config: dict[str, Any],
        enabled: bool = True,
        priority: int = 100,
    ) -> Policy:
        """Create a new governance policy."""
        policy_id = uuid.uuid4().hex[:24]
        now = datetime.now(tz=timezone.utc)

        if isinstance(policy_type, str):
            policy_type = PolicyType(policy_type)
        if isinstance(action, str):
            action = PolicyAction(action)

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO governance_policies "
            "(policy_id, tenant_id, name, description, policy_type, action, "
            " config, enabled, priority, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                policy_id, tenant_id, name, description,
                policy_type.value, action.value,
                json.dumps(config), enabled, priority, now, now,
            ),
        )

        logger.info("Policy created: %s (%s) for tenant %s", name, policy_type.value, tenant_id)

        return Policy(
            policy_id=policy_id,
            tenant_id=tenant_id,
            name=name,
            description=description,
            policy_type=policy_type,
            action=action,
            config=config,
            enabled=enabled,
            priority=priority,
            created_at=now,
            updated_at=now,
        )

    def get_policy(self, policy_id: str) -> Policy | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM governance_policies WHERE policy_id = %s",
            (policy_id,),
        ).fetchone()
        return self._row_to_policy(row) if row else None

    def list_policies(
        self,
        tenant_id: str,
        enabled_only: bool = False,
    ) -> list[Policy]:
        conn = self._get_conn()
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM governance_policies "
                "WHERE tenant_id = %s AND enabled = TRUE "
                "ORDER BY priority ASC, created_at ASC",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM governance_policies "
                "WHERE tenant_id = %s ORDER BY priority ASC, created_at ASC",
                (tenant_id,),
            ).fetchall()
        return [self._row_to_policy(r) for r in rows]

    def redact(self, tenant_id: str, text: str) -> str:
        """Apply PII redaction rules to the input text."""
        if not text:
            return text
            
        policies = self.list_policies(tenant_id, enabled_only=True)
        for policy in policies:
            if policy.policy_type == "pii_guard" and policy.action == "redact":
                patterns = policy.config.get("patterns", [])
                for pattern in patterns:
                    try:
                        import re
                        text = re.sub(pattern, "[REDACTED]", text)
                    except Exception:
                        pass
        return text

    def update_policy(
        self,
        policy_id: str,
        tenant_id: str,
        **kwargs,
    ) -> Policy | None:
        """Update a policy. Only provided fields are changed."""
        existing = self.get_policy(policy_id)
        if existing is None or existing.tenant_id != tenant_id:
            return None

        allowed_fields = {"name", "description", "action", "config", "enabled", "priority"}
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields and v is not None}

        if not updates:
            return existing

        # Convert enums
        if "action" in updates and isinstance(updates["action"], str):
            updates["action"] = PolicyAction(updates["action"]).value
        elif "action" in updates:
            updates["action"] = updates["action"].value

        if "config" in updates and isinstance(updates["config"], dict):
            updates["config"] = json.dumps(updates["config"])

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        set_clause += ", updated_at = now()"
        values = list(updates.values()) + [policy_id, tenant_id]

        conn = self._get_conn()
        conn.execute(
            f"UPDATE governance_policies SET {set_clause} "
            "WHERE policy_id = %s AND tenant_id = %s",
            values,
        )

        return self.get_policy(policy_id)

    def delete_policy(self, policy_id: str, tenant_id: str) -> bool:
        conn = self._get_conn()
        result = conn.execute(
            "DELETE FROM governance_policies "
            "WHERE policy_id = %s AND tenant_id = %s",
            (policy_id, tenant_id),
        )
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Policy Evaluation — delegates to PolicyEngine + EvidenceCollector
    # ------------------------------------------------------------------

    def evaluate(
        self,
        tenant_id: str,
        operation: str,
        context: dict[str, Any],
    ) -> list[EvaluationLog]:
        """Evaluate all active policies against a request.

        Delegates to PolicyEngine for verdicts and EvidenceCollector
        for persistence.  Public API unchanged.
        """
        policies = self.list_policies(tenant_id, enabled_only=True)
        verdicts = self._engine.evaluate(policies, operation, context)
        logs = self._evidence.log_verdicts(
            tenant_id, verdicts, operation, context,
        )
        return logs

    def evaluate_and_gate(
        self,
        tenant_id: str,
        operation: str,
        context: dict[str, Any],
    ) -> tuple[bool, list[EvaluationLog]]:
        """Evaluate and return (allowed, logs).

        Returns False if any policy denied or escalated.
        Dispatches webhook alerts for denials and escalations.
        """
        logs = self.evaluate(tenant_id, operation, context)
        denied = any(log.result == EvaluationResult.DENIED for log in logs)
        escalated = any(log.result == EvaluationResult.ESCALATED for log in logs)

        # Dispatch webhook alerts for governance events
        wh = getattr(self, "_webhook_service", None)
        if wh is not None:
            for log in logs:
                if log.result == EvaluationResult.DENIED:
                    wh.dispatch(tenant_id, "governance.denied", {
                        "policy_id": log.policy_id,
                        "policy_name": log.policy_name,
                        "operation": log.operation,
                        "detail": log.detail,
                    })
                elif log.result == EvaluationResult.ESCALATED:
                    wh.dispatch(tenant_id, "governance.escalated", {
                        "policy_id": log.policy_id,
                        "policy_name": log.policy_name,
                        "operation": log.operation,
                        "detail": log.detail,
                    })

        try:
            from aml.cloud.metrics import GOVERNANCE_EVALUATIONS
            if escalated:
                GOVERNANCE_EVALUATIONS.labels(result="escalate").inc()
            elif denied:
                GOVERNANCE_EVALUATIONS.labels(result="deny").inc()
            else:
                GOVERNANCE_EVALUATIONS.labels(result="allow").inc()
        except Exception:
            pass

        return (not denied and not escalated), logs

    # ── Legacy bridge (kept for backward compatibility) ────────

    def _evaluate_single(
        self,
        policy: Policy,
        operation: str,
        context: dict[str, Any],
    ) -> tuple[EvaluationResult, str]:
        """Evaluate a single policy via PolicyEngine. Legacy bridge."""
        verdict = self._engine.evaluate_single(policy, operation, context)
        return verdict.result, verdict.detail

    def _action_to_result(self, action: PolicyAction) -> EvaluationResult:
        return self._engine._action_to_result(action)

    def _summarize_request(self, operation: str, context: dict) -> str:
        return self._evidence._summarize_request(operation, context)

    def _persist_log(self, log: EvaluationLog) -> None:
        """Legacy bridge — delegates to EvidenceCollector."""
        from aml.cloud.policy_engine import Verdict
        verdict = Verdict(
            policy_id=log.policy_id,
            policy_name=log.policy_name,
            result=log.result,
            detail=log.detail,
            policy_type="",
            action="",
        )
        self._evidence.log_evaluation(
            log.tenant_id, verdict, log.operation, {},
        )

    def get_logs(
        self,
        tenant_id: str,
        policy_id: str | None = None,
        result: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EvaluationLog]:
        """Delegates to EvidenceCollector."""
        return self._evidence.get_logs(
            tenant_id, policy_id=policy_id, result=result,
            limit=limit, offset=offset,
        )

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        """Combines policy stats (local) with evaluation stats (evidence)."""
        conn = self._get_conn()

        pol_row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "  COUNT(CASE WHEN enabled THEN 1 END) AS active "
            "FROM governance_policies WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        eval_stats = self._evidence.get_stats(tenant_id)

        return {
            "policies_total": pol_row["total"] if pol_row else 0,
            "policies_active": pol_row["active"] if pol_row else 0,
            **eval_stats,
        }

    # ------------------------------------------------------------------
    # Seed default policies
    # ------------------------------------------------------------------

    def seed_defaults(self, tenant_id: str) -> int:
        """Create default policies for a new tenant. Returns count created."""
        existing = self.list_policies(tenant_id)
        if existing:
            return 0  # Already seeded

        count = 0
        for d in DEFAULT_POLICIES:
            self.create_policy(
                tenant_id=tenant_id,
                name=d["name"],
                description=d["description"],
                policy_type=d["policy_type"],
                action=d["action"],
                config=d["config"],
                priority=d["priority"],
            )
            count += 1

        logger.info("Seeded %d default policies for tenant %s", count, tenant_id)
        return count

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_policy(row: dict[str, Any]) -> Policy:
        cfg = row.get("config")
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        elif cfg is None:
            cfg = {}

        return Policy(
            policy_id=row["policy_id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            description=row.get("description", ""),
            policy_type=PolicyType(row["policy_type"]),
            action=PolicyAction(row["action"]),
            config=cfg,
            enabled=row["enabled"],
            priority=row["priority"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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
    def policy_to_dict(p: Policy) -> dict[str, Any]:
        return {
            "policy_id": p.policy_id,
            "tenant_id": p.tenant_id,
            "name": p.name,
            "description": p.description,
            "policy_type": p.policy_type.value,
            "action": p.action.value,
            "config": p.config,
            "enabled": p.enabled,
            "priority": p.priority,
            "created_at": p.created_at.isoformat(),
            "updated_at": p.updated_at.isoformat(),
        }

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
