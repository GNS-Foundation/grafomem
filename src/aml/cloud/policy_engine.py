"""
GRAFOMEM Policy Engine — stateless policy evaluation (PDP).

Implements the Policy Decision Point (PDP) in an OPA-style architecture:
the engine evaluates rules and produces verdicts; the Governance Gateway
(PEP) enforces them.

Extracted from GovernanceGateway to enable:
  - Independent testing with property-based tests
  - Pure-function semantics: same policies + same context = same verdict
  - Future: policy composition, external policy sources, Rego integration

Backed by in-memory state only (rate-limit counters). No database access.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from aml.cloud.governance import (
    EvaluationResult,
    Policy,
    PolicyAction,
    PolicyType,
)

logger = logging.getLogger("grafomem.cloud.policy_engine")


# ============================================================================
# Verdict — the output of a single policy evaluation
# ============================================================================

@dataclass(slots=True)
class Verdict:
    """Result of evaluating a single policy against a request context."""
    policy_id: str
    policy_name: str
    result: EvaluationResult
    detail: str
    policy_type: str
    action: str


# ============================================================================
# PolicyEngine — pure evaluation, no side effects
# ============================================================================

class PolicyEngine:
    """Stateless policy evaluation engine (Policy Decision Point).

    Evaluates a list of policies against an operation context and returns
    a list of verdicts. Does not persist anything — that's the job of
    EvidenceCollector. Does not enforce — that's the GovernanceGateway.

    The only mutable state is ``_rate_counters`` for rate-limit tracking,
    which is inherently stateful (time-windowed counters).
    """

    def __init__(self) -> None:
        self._rate_counters: dict[tuple[str, str], list[float]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        policies: list[Policy],
        operation: str,
        context: dict[str, Any],
    ) -> list[Verdict]:
        """Evaluate all policies and return one verdict per policy.

        Parameters
        ----------
        policies : list[Policy]
            Active policies, ordered by priority.
        operation : str
            The operation being attempted (e.g. "write", "retrieve", "inference").
        context : dict
            Request context. Keys depend on policy type:
            - model_id, store_id, query, output, tokens.

        Returns
        -------
        list[Verdict]
            One verdict per policy, in evaluation order.
        """
        return [self.evaluate_single(p, operation, context) for p in policies]

    def evaluate_single(
        self,
        policy: Policy,
        operation: str,
        context: dict[str, Any],
    ) -> Verdict:
        """Evaluate a single policy against a context."""
        try:
            result, detail = self._dispatch(policy, operation, context)
        except Exception as e:
            logger.error("Policy evaluation error: %s — %s", policy.name, e)
            result = EvaluationResult.ALLOWED
            detail = f"Evaluation error (fail-open): {e}"

        return Verdict(
            policy_id=policy.policy_id,
            policy_name=policy.name,
            result=result,
            detail=detail,
            policy_type=policy.policy_type.value,
            action=policy.action.value,
        )

    def explain(self, verdicts: list[Verdict]) -> str:
        """Human-readable summary of evaluation results."""
        if not verdicts:
            return "No policies evaluated."

        lines = []
        denied = [v for v in verdicts if v.result == EvaluationResult.DENIED]
        escalated = [v for v in verdicts if v.result == EvaluationResult.ESCALATED]
        logged = [v for v in verdicts if v.result == EvaluationResult.LOGGED]

        if denied:
            lines.append(f"DENIED by {len(denied)} policy(ies):")
            for v in denied:
                lines.append(f"  • {v.policy_name}: {v.detail}")
        if escalated:
            lines.append(f"ESCALATED by {len(escalated)} policy(ies):")
            for v in escalated:
                lines.append(f"  • {v.policy_name}: {v.detail}")
        if logged:
            lines.append(f"LOGGED by {len(logged)} policy(ies):")
            for v in logged:
                lines.append(f"  • {v.policy_name}: {v.detail}")

        allowed = len(verdicts) - len(denied) - len(escalated) - len(logged)
        if allowed > 0:
            lines.append(f"{allowed} policy(ies) allowed.")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        policy: Policy,
        operation: str,
        context: dict[str, Any],
    ) -> tuple[EvaluationResult, str]:
        """Route to the correct evaluator based on policy type."""
        evaluators = {
            PolicyType.RATE_LIMIT: self._eval_rate_limit,
            PolicyType.MODEL_ALLOWLIST: self._eval_model_allowlist,
            PolicyType.CONTENT_FILTER: self._eval_content_filter,
            PolicyType.DATA_SCOPE: self._eval_data_scope,
            PolicyType.TOKEN_BUDGET: self._eval_token_budget,
            PolicyType.HITL_REQUIRED: lambda p, c: self._eval_hitl(p, operation, c),
            PolicyType.PII_GUARD: self._eval_pii_guard,
            PolicyType.WORLD_MODEL_CONSTRAINT: self._eval_world_model_constraint,
            PolicyType.TOOL_DENY: self._eval_tool_deny,
        }
        evaluator = evaluators.get(policy.policy_type)
        if evaluator is None:
            return EvaluationResult.ALLOWED, f"Unknown policy type: {policy.policy_type}"
        return evaluator(policy, context)

    # ------------------------------------------------------------------
    # Evaluators
    # ------------------------------------------------------------------

    def _eval_rate_limit(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        max_req = policy.config.get("max_requests", 600)
        window = policy.config.get("window_seconds", 60)
        now = time.monotonic()

        key = (policy.tenant_id, policy.policy_id)
        timestamps = self._rate_counters.get(key, [])
        cutoff = now - window
        timestamps = [t for t in timestamps if t > cutoff]
        timestamps.append(now)
        self._rate_counters[key] = timestamps

        if len(timestamps) > max_req:
            return self._action_to_result(policy.action), (
                f"Rate limit exceeded: {len(timestamps)}/{max_req} in {window}s"
            )
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
        return self._action_to_result(policy.action), (
            f"Model '{model_id}' not in allowlist: {allowed_models}"
        )

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
                        return self._action_to_result(policy.action), (
                            f"Content filter match in '{field_name}': pattern '{pattern}'"
                        )
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
        return self._action_to_result(policy.action), (
            f"Store '{store_id}' outside allowed scope: {allowed_stores}"
        )

    def _eval_token_budget(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        max_tokens = policy.config.get("max_tokens_per_request", 10000)
        tokens = context.get("tokens", 0)
        if tokens <= max_tokens:
            return EvaluationResult.ALLOWED, f"Token budget OK: {tokens}/{max_tokens}"
        return self._action_to_result(policy.action), (
            f"Token budget exceeded: {tokens}/{max_tokens}"
        )

    def _eval_hitl(
        self, policy: Policy, operation: str, context: dict,
    ) -> tuple[EvaluationResult, str]:
        operations = policy.config.get("operations", [])
        if not operations or operation in operations:
            return EvaluationResult.ESCALATED, (
                f"HITL required for '{operation}' — awaiting human approval"
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
                        findings.append(
                            f"{field_name}: {len(matches)} match(es) for '{pattern}'"
                        )
                except re.error:
                    continue
        if findings:
            return self._action_to_result(policy.action), (
                f"PII detected: {'; '.join(findings)}"
            )
        return EvaluationResult.ALLOWED, "No PII detected"

    def _eval_world_model_constraint(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        # 1. Require params generic check
        require_params = policy.config.get("require_params", [])
        if require_params:
            for_actions = policy.config.get("for_actions", [])
            if not for_actions or context.get("action") in for_actions:
                params = context.get("params", {})
                for p in require_params:
                    if not params.get(p):
                        return self._action_to_result(policy.action), f"Missing required parameter '{p}'"

        # 2. Generic Declarative Constraints
        deny_if = policy.config.get("deny_if")
        if deny_if:
            # Check if this rule applies to the current action
            if deny_if.get("action") and context.get("action") != deny_if.get("action"):
                return EvaluationResult.ALLOWED, "Action does not match constraint"
            
            params = context.get("params", {})
            params_has = deny_if.get("params_has", [])
            
            # All conditions in params_has must be met by AT LEAST ONE item in the list
            all_conditions_met = True
            for condition in params_has:
                list_path = condition.get("list_path")
                match_all = condition.get("match_all", [])
                
                items = params.get(list_path, [])
                if not isinstance(items, list):
                    all_conditions_met = False
                    break
                    
                # Does at least one item match all criteria?
                condition_met_by_item = False
                for item in items:
                    item_matches = True
                    for criterion in match_all:
                        field = criterion.get("field")
                        op = criterion.get("operator")
                        val = criterion.get("value")
                        item_val = item.get(field)
                        
                        if op == "==" and item_val != val:
                            item_matches = False
                        elif op == ">" and not (isinstance(item_val, (int, float)) and item_val > val):
                            item_matches = False
                        elif op == "<" and not (isinstance(item_val, (int, float)) and item_val < val):
                            item_matches = False
                        elif op == "contains" and not (isinstance(item_val, list) and val in item_val):
                            item_matches = False
                        
                        if not item_matches:
                            break
                            
                    if item_matches:
                        condition_met_by_item = True
                        break
                        
                if not condition_met_by_item:
                    all_conditions_met = False
                    break
                    
            if all_conditions_met and params_has:
                return self._action_to_result(policy.action), "Stateful constraint violated"

        return EvaluationResult.ALLOWED, "Constraints satisfied"

    def _eval_tool_deny(
        self, policy: Policy, context: dict,
    ) -> tuple[EvaluationResult, str]:
        """Deny specific tools by name. Native tool-execution-deny policy.

        Config: {"denied_tools": ["tool_name_1", "tool_pattern_*"]}
        Context: {"tool_name": "...", ...}  (passed by orchestrator for op=tool_execution)
        """
        denied_tools = policy.config.get("denied_tools", [])
        tool_name = context.get("tool_name", "")
        if not tool_name or not denied_tools:
            return EvaluationResult.ALLOWED, "No tool deny restriction"

        for pattern in denied_tools:
            # Exact match first, then regex fallback
            if pattern == tool_name:
                return self._action_to_result(policy.action), (
                    f"Tool '{tool_name}' denied by policy: exact match '{pattern}'"
                )
            try:
                if re.fullmatch(pattern, tool_name):
                    return self._action_to_result(policy.action), (
                        f"Tool '{tool_name}' denied by policy: pattern match '{pattern}'"
                    )
            except re.error:
                continue
        return EvaluationResult.ALLOWED, f"Tool '{tool_name}' not in deny list"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _action_to_result(action: PolicyAction) -> EvaluationResult:
        mapping = {
            PolicyAction.DENY: EvaluationResult.DENIED,
            PolicyAction.ALLOW: EvaluationResult.ALLOWED,
            PolicyAction.ESCALATE: EvaluationResult.ESCALATED,
            PolicyAction.LOG_ONLY: EvaluationResult.LOGGED,
        }
        return mapping.get(action, EvaluationResult.DENIED)
