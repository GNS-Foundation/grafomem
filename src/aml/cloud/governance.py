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
    """Policy-as-code engine for constraining agent behavior.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    """

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        # In-memory rate limit counters: { (tenant_id, policy_id): [timestamps] }
        self._rate_counters: dict[tuple[str, str], list[float]] = {}

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
        conn.execute(_SCHEMA_SQL)
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
    # Policy Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        tenant_id: str,
        operation: str,
        context: dict[str, Any],
    ) -> list[EvaluationLog]:
        """Evaluate all active policies against a request.

        Parameters
        ----------
        tenant_id : str
            The tenant performing the operation.
        operation : str
            The operation type (e.g. "write", "retrieve", "inference").
        context : dict
            Request context. Keys depend on policy type:
            - model_id: str (for model_allowlist)
            - store_id: str (for data_scope)
            - query: str (for content_filter)
            - output: str (for content_filter, pii_guard)
            - tokens: int (for token_budget)

        Returns
        -------
        list[EvaluationLog]
            One log entry per evaluated policy.
        """
        policies = self.list_policies(tenant_id, enabled_only=True)
        logs: list[EvaluationLog] = []

        for policy in policies:
            result, detail = self._evaluate_single(policy, operation, context)

            log_entry = EvaluationLog(
                log_id=uuid.uuid4().hex[:24],
                tenant_id=tenant_id,
                policy_id=policy.policy_id,
                policy_name=policy.name,
                result=result,
                operation=operation,
                detail=detail,
                request_summary=self._summarize_request(operation, context),
            )
            logs.append(log_entry)

            # Persist the log
            self._persist_log(log_entry)

        return logs

    def evaluate_and_gate(
        self,
        tenant_id: str,
        operation: str,
        context: dict[str, Any],
    ) -> tuple[bool, list[EvaluationLog]]:
        """Evaluate and return (allowed, logs).

        Returns False if any policy denied the request.
        """
        logs = self.evaluate(tenant_id, operation, context)
        denied = any(log.result == EvaluationResult.DENIED for log in logs)
        escalated = any(log.result == EvaluationResult.ESCALATED for log in logs)
        return (not denied and not escalated), logs

    def _evaluate_single(
        self,
        policy: Policy,
        operation: str,
        context: dict[str, Any],
    ) -> tuple[EvaluationResult, str]:
        """Evaluate a single policy. Returns (result, detail)."""
        try:
            if policy.policy_type == PolicyType.RATE_LIMIT:
                return self._eval_rate_limit(policy, context)
            elif policy.policy_type == PolicyType.MODEL_ALLOWLIST:
                return self._eval_model_allowlist(policy, context)
            elif policy.policy_type == PolicyType.CONTENT_FILTER:
                return self._eval_content_filter(policy, context)
            elif policy.policy_type == PolicyType.DATA_SCOPE:
                return self._eval_data_scope(policy, context)
            elif policy.policy_type == PolicyType.TOKEN_BUDGET:
                return self._eval_token_budget(policy, context)
            elif policy.policy_type == PolicyType.HITL_REQUIRED:
                return self._eval_hitl(policy, operation, context)
            elif policy.policy_type == PolicyType.PII_GUARD:
                return self._eval_pii_guard(policy, context)
            else:
                return EvaluationResult.ALLOWED, f"Unknown policy type: {policy.policy_type}"
        except Exception as e:
            logger.error("Policy evaluation error: %s — %s", policy.name, e)
            return EvaluationResult.ALLOWED, f"Evaluation error (fail-open): {e}"

    # ── Evaluators ────────────────────────────────────────────

    def _eval_rate_limit(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        max_req = policy.config.get("max_requests", 600)
        window = policy.config.get("window_seconds", 60)
        now = time.monotonic()

        key = (policy.tenant_id, policy.policy_id)
        timestamps = self._rate_counters.get(key, [])

        # Prune expired entries
        cutoff = now - window
        timestamps = [t for t in timestamps if t > cutoff]
        timestamps.append(now)
        self._rate_counters[key] = timestamps

        if len(timestamps) > max_req:
            action_result = self._action_to_result(policy.action)
            return action_result, f"Rate limit exceeded: {len(timestamps)}/{max_req} in {window}s"

        return EvaluationResult.ALLOWED, f"Rate OK: {len(timestamps)}/{max_req}"

    def _eval_model_allowlist(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        allowed_models = policy.config.get("models", [])
        model_id = context.get("model_id", "")

        if not model_id:
            return EvaluationResult.ALLOWED, "No model_id in request"

        if not allowed_models:
            return EvaluationResult.ALLOWED, "No model restrictions configured"

        if model_id in allowed_models:
            return EvaluationResult.ALLOWED, f"Model '{model_id}' is allowed"

        action_result = self._action_to_result(policy.action)
        return action_result, f"Model '{model_id}' not in allowlist: {allowed_models}"

    def _eval_content_filter(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        patterns = policy.config.get("patterns", [])
        check_fields = policy.config.get("check_fields", ["query", "output"])

        for field_name in check_fields:
            text = context.get(field_name, "")
            if not text:
                continue
            for pattern in patterns:
                try:
                    if re.search(pattern, text, re.IGNORECASE):
                        action_result = self._action_to_result(policy.action)
                        return action_result, f"Content filter match in '{field_name}': pattern '{pattern}'"
                except re.error:
                    continue

        return EvaluationResult.ALLOWED, "No content filter matches"

    def _eval_data_scope(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        allowed_stores = policy.config.get("allowed_stores", [])
        store_id = context.get("store_id", "")

        if not store_id or not allowed_stores:
            return EvaluationResult.ALLOWED, "No data scope restriction"

        if store_id in allowed_stores:
            return EvaluationResult.ALLOWED, f"Store '{store_id}' is in scope"

        action_result = self._action_to_result(policy.action)
        return action_result, f"Store '{store_id}' outside allowed scope: {allowed_stores}"

    def _eval_token_budget(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        max_tokens = policy.config.get("max_tokens_per_request", 10000)
        tokens = context.get("tokens", 0)

        if tokens <= max_tokens:
            return EvaluationResult.ALLOWED, f"Token budget OK: {tokens}/{max_tokens}"

        action_result = self._action_to_result(policy.action)
        return action_result, f"Token budget exceeded: {tokens}/{max_tokens}"

    def _eval_hitl(
        self, policy: Policy, operation: str, context: dict,
    ) -> tuple[EvaluationResult, str]:
        operations = policy.config.get("operations", [])
        if not operations or operation in operations:
            return EvaluationResult.ESCALATED, (
                f"HITL required for '{operation}' — "
                "awaiting human approval"
            )
        return EvaluationResult.ALLOWED, f"Operation '{operation}' not subject to HITL"

    def _eval_pii_guard(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        patterns = policy.config.get("patterns", [])
        check_fields = policy.config.get("check_fields", ["output"])
        findings: list[str] = []

        for field_name in check_fields:
            text = context.get(field_name, "")
            if not text:
                continue
            for pattern in patterns:
                try:
                    matches = re.findall(pattern, text)
                    if matches:
                        findings.append(f"{field_name}: {len(matches)} match(es) for '{pattern}'")
                except re.error:
                    continue

        if findings:
            action_result = self._action_to_result(policy.action)
            return action_result, f"PII detected: {'; '.join(findings)}"

        return EvaluationResult.ALLOWED, "No PII detected"

    # ── Helpers ────────────────────────────────────────────────

    def _action_to_result(self, action: PolicyAction) -> EvaluationResult:
        mapping = {
            PolicyAction.DENY: EvaluationResult.DENIED,
            PolicyAction.ALLOW: EvaluationResult.ALLOWED,
            PolicyAction.ESCALATE: EvaluationResult.ESCALATED,
            PolicyAction.LOG_ONLY: EvaluationResult.LOGGED,
        }
        return mapping.get(action, EvaluationResult.DENIED)

    def _summarize_request(self, operation: str, context: dict) -> str:
        parts = [f"op={operation}"]
        if context.get("model_id"):
            parts.append(f"model={context['model_id']}")
        if context.get("store_id"):
            parts.append(f"store={context['store_id']}")
        if context.get("query"):
            q = context["query"]
            parts.append(f"query=\"{q[:50]}{'…' if len(q) > 50 else ''}\"")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Evaluation logs
    # ------------------------------------------------------------------

    def _persist_log(self, log: EvaluationLog) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO governance_evaluation_log "
            "(log_id, tenant_id, policy_id, policy_name, result, "
            " operation, detail, request_summary, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                log.log_id, log.tenant_id, log.policy_id, log.policy_name,
                log.result.value, log.operation, log.detail,
                log.request_summary, log.created_at,
            ),
        )

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

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        conn = self._get_conn()

        # Policy stats
        pol_row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "  COUNT(CASE WHEN enabled THEN 1 END) AS active "
            "FROM governance_policies WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        # Evaluation stats
        eval_row = conn.execute(
            "SELECT COUNT(*) AS total_evals, "
            "  COUNT(CASE WHEN result = 'denied' THEN 1 END) AS denied, "
            "  COUNT(CASE WHEN result = 'escalated' THEN 1 END) AS escalated, "
            "  COUNT(CASE WHEN result = 'logged' THEN 1 END) AS logged, "
            "  COUNT(CASE WHEN result = 'allowed' THEN 1 END) AS allowed "
            "FROM governance_evaluation_log WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        return {
            "policies_total": pol_row["total"] if pol_row else 0,
            "policies_active": pol_row["active"] if pol_row else 0,
            "evaluations_total": eval_row["total_evals"] if eval_row else 0,
            "evaluations_denied": eval_row["denied"] if eval_row else 0,
            "evaluations_escalated": eval_row["escalated"] if eval_row else 0,
            "evaluations_logged": eval_row["logged"] if eval_row else 0,
            "evaluations_allowed": eval_row["allowed"] if eval_row else 0,
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
