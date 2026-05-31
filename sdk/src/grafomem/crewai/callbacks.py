"""GRAFOMEM governance callbacks for CrewAI.

Provides task-level governance hooks that evaluate GRAFOMEM policies
before and after each CrewAI task execution.

Usage::

    from grafomem import GrafomemClient
    from grafomem.crewai import GrafomemGovernanceCallback

    client = GrafomemClient(api_key="gfm_...")
    callback = GrafomemGovernanceCallback(client=client)

    task = Task(
        description="Research compliance requirements",
        callbacks=[callback],
        agent=researcher,
    )
"""
from __future__ import annotations

import logging
import time
from typing import Any

from grafomem.client import GrafomemClient

logger = logging.getLogger("grafomem.crewai.governance")


class GovernanceDeniedError(Exception):
    """Raised when a governance policy denies task execution."""
    def __init__(self, reason: str, policy_id: str | None = None):
        self.reason = reason
        self.policy_id = policy_id
        super().__init__(f"Governance denied: {reason}")


class GrafomemGovernanceCallback:
    """CrewAI task callback with GRAFOMEM governance pre/post checks.

    Before each task runs, evaluates GRAFOMEM governance policies.
    After each task completes, logs the decision to the decision trail.
    If a policy denies execution, raises GovernanceDeniedError.

    Args:
        client: A GrafomemClient instance.
        action: The governance action to evaluate (default "inference").
        deny_raises: If True (default), denied actions raise GovernanceDeniedError.
            If False, denied actions are logged but execution continues.
    """

    def __init__(
        self,
        client: GrafomemClient,
        *,
        action: str = "inference",
        deny_raises: bool = True,
    ) -> None:
        self._client = client
        self._action = action
        self._deny_raises = deny_raises

    def on_task_start(self, task_description: str, **kwargs: Any) -> None:
        """Pre-task governance check."""
        context = {
            "task_description": task_description,
            "source": "crewai",
            "timestamp": time.time(),
            **kwargs,
        }
        try:
            result = self._client.governance.evaluate(self._action, context)
            verdict = result.get("verdict", "allow")
            if verdict == "deny" and self._deny_raises:
                raise GovernanceDeniedError(
                    reason=result.get("reason", "Policy denied"),
                    policy_id=result.get("policy_id"),
                )
            logger.info("Governance check: %s for task: %.60s", verdict, task_description)
        except GovernanceDeniedError:
            raise
        except Exception as e:
            logger.warning("Governance check failed (allowing): %s", e)

    def on_task_end(self, output: str, **kwargs: Any) -> None:
        """Post-task decision logging."""
        try:
            self._client.decisions.log(
                action=self._action,
                output=output[:2000],  # Truncate large outputs
                source="crewai",
                meta={"type": "crew_task_completion", **kwargs},
            )
        except Exception as e:
            logger.warning("Decision logging failed: %s", e)
