"""Governance service.

Provides policy CRUD, evaluation, and audit log access.

Usage::

    policy = client.governance.create_policy(
        name="PII Guard", policy_type="pii_guard", action="deny",
        config={"patterns": [r"\\b\\d{3}-\\d{2}-\\d{4}\\b"]},
    )
    result = client.governance.evaluate(
        operation="output_check", context={"output": text},
    )
    if not result.allowed:
        print(f"Blocked by: {result.logs}")
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from grafomem.types import (
    EvaluationLog,
    EvaluationResult,
    GovernanceStats,
    Policy,
)

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class GovernanceService:
    """Policy-as-code governance gateway."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    # ── Policy CRUD ───────────────────────────────────────────────────

    def create_policy(
        self,
        name: str,
        policy_type: str,
        action: str = "deny",
        *,
        config: dict[str, Any] | None = None,
        description: str = "",
        priority: int = 0,
        enabled: bool = True,
        **kwargs: Any,
    ) -> Policy:
        """Create a governance policy.

        Args:
            name: Human-readable policy name.
            policy_type: One of ``rate_limit``, ``model_allowlist``,
                ``content_filter``, ``data_scope``, ``token_budget``,
                ``hitl_required``, ``pii_guard``.
            action: ``deny``, ``escalate``, ``log_only``, or ``allow``.
            config: Policy-type-specific configuration.
            description: Optional description.
            priority: Evaluation priority (lower = evaluated first).
            enabled: Whether the policy is active.

        Returns:
            The created :class:`Policy`.
        """
        body: dict[str, Any] = {
            "name": name,
            "policy_type": policy_type,
            "action": action,
            "config": config or {},
            "description": description,
            "priority": priority,
            "enabled": enabled,
            **kwargs,
        }
        data = self._http.post("/v1/governance/policies", json=body)
        return Policy.model_validate(data)

    def list_policies(self) -> list[Policy]:
        """List all governance policies."""
        data = self._http.get("/v1/governance/policies")
        items = data if isinstance(data, list) else data.get("policies", [])
        return [Policy.model_validate(p) for p in items]

    def get_policy(self, policy_id: str) -> Policy:
        """Get a single policy by ID."""
        data = self._http.get(f"/v1/governance/policies/{policy_id}")
        return Policy.model_validate(data)

    def update_policy(self, policy_id: str, **fields: Any) -> Policy:
        """Update a policy. Pass only the fields to change.

        Args:
            policy_id: The policy ID.
            **fields: Fields to update (e.g. ``enabled=False``).

        Returns:
            The updated :class:`Policy`.
        """
        data = self._http.put(f"/v1/governance/policies/{policy_id}", json=fields)
        return Policy.model_validate(data)

    def delete_policy(self, policy_id: str) -> None:
        """Delete a policy."""
        self._http.delete(f"/v1/governance/policies/{policy_id}")

    # ── Evaluation ────────────────────────────────────────────────────

    def evaluate(
        self,
        operation: str,
        context: dict[str, Any] | None = None,
    ) -> EvaluationResult:
        """Evaluate all active policies against a request.

        Args:
            operation: The operation type (e.g. ``"inference"``,
                ``"output_check"``, ``"deploy"``).
            context: Operation-specific context (e.g. ``{"model_id": "gpt-4o"}``).

        Returns:
            An :class:`EvaluationResult` with ``allowed``, ``logs``,
            and ``escalated`` fields.
        """
        body: dict[str, Any] = {"operation": operation}
        if context:
            body["context"] = context

        data = self._http.post("/v1/governance/evaluate", json=body)
        return EvaluationResult.model_validate(data)

    # ── Metadata ──────────────────────────────────────────────────────

    def policy_types(self) -> list[dict[str, Any]]:
        """Get available policy types with their config schemas."""
        data = self._http.get("/v1/governance/policy-types")
        return data if isinstance(data, list) else data.get("types", [])

    def logs(self, *, limit: int = 50) -> list[EvaluationLog]:
        """Get recent governance evaluation logs."""
        data = self._http.get("/v1/governance/logs", params={"limit": limit})
        items = data if isinstance(data, list) else data.get("logs", [])
        return [EvaluationLog.model_validate(log) for log in items]

    def stats(self) -> GovernanceStats | dict:
        """Get governance summary statistics."""
        data = self._http.get("/v1/governance/stats")
        if data is None:
            return GovernanceStats()
        try:
            return GovernanceStats.model_validate(data)
        except Exception:
            return data
