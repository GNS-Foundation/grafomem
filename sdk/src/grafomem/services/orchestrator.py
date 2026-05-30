"""Orchestrator service.

Provides agent definitions, workflow execution, receipts, replay, and
chain verification.

Usage::

    agent = client.orchestrator.create_agent(
        name="researcher", role="researcher", model_id="gpt-4o-mini",
    )
    workflow = client.orchestrator.create_workflow(
        name="research-flow", agents=[agent.id],
    )
    run = client.orchestrator.run_workflow(workflow.id, input="Analyze Q2")
    print(run.status, run.total_tokens)
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from grafomem.types import (
    Agent,
    ChainVerification,
    OrchestratorStats,
    Receipt,
    ReplayResult,
    StreamEvent,
    Workflow,
    WorkflowRun,
)

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class OrchestratorService:
    """Governed multi-agent orchestration."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    # ── Agent CRUD ────────────────────────────────────────────────────

    def create_agent(
        self,
        name: str,
        role: str,
        model_id: str,
        *,
        system_prompt: str = "",
        tools: list[str] | None = None,
        store_id: str | None = None,
        **kwargs: Any,
    ) -> Agent:
        """Create an agent definition.

        Args:
            name: Agent name.
            role: Agent role (e.g. ``"researcher"``, ``"writer"``).
            model_id: LLM model identifier.
            system_prompt: System prompt for the agent.
            tools: List of tool names the agent can use.
            store_id: Memory store ID for the agent.

        Returns:
            The created :class:`Agent`.
        """
        body: dict[str, Any] = {
            "name": name,
            "role": role,
            "model_id": model_id,
            **kwargs,
        }
        if system_prompt:
            body["system_prompt"] = system_prompt
        if tools:
            body["tools"] = tools
        if store_id:
            body["store_id"] = store_id

        data = self._http.post("/v1/orchestrator/agents", json=body)
        return Agent.model_validate(data)

    def list_agents(self) -> list[Agent]:
        """List all agent definitions."""
        data = self._http.get("/v1/orchestrator/agents")
        items = data if isinstance(data, list) else data.get("agents", [])
        return [Agent.model_validate(a) for a in items]

    def get_agent(self, agent_id: str) -> Agent:
        """Get a single agent definition."""
        data = self._http.get(f"/v1/orchestrator/agents/{agent_id}")
        return Agent.model_validate(data)

    def update_agent(self, agent_id: str, **fields: Any) -> Agent:
        """Update an agent definition."""
        data = self._http.put(f"/v1/orchestrator/agents/{agent_id}", json=fields)
        return Agent.model_validate(data)

    def delete_agent(self, agent_id: str) -> None:
        """Delete an agent definition."""
        self._http.delete(f"/v1/orchestrator/agents/{agent_id}")

    # ── Workflow CRUD + Execution ─────────────────────────────────────

    def create_workflow(
        self,
        name: str,
        agents: list[str],
        *,
        mode: str = "sequential",
        input_text: str = "",
        **kwargs: Any,
    ) -> Workflow:
        """Create a workflow definition.

        Args:
            name: Workflow name.
            agents: Ordered list of agent IDs.
            mode: Execution mode (``"sequential"``).
            input_text: Initial input for the workflow.

        Returns:
            The created :class:`Workflow`.
        """
        body: dict[str, Any] = {
            "name": name,
            "agent_ids": agents,
            "mode": mode,
            **kwargs,
        }

        data = self._http.post("/v1/orchestrator/workflows", json=body)
        return Workflow.model_validate(data)

    def list_workflows(self) -> list[Workflow]:
        """List all workflows."""
        data = self._http.get("/v1/orchestrator/workflows")
        items = data if isinstance(data, list) else data.get("workflows", [])
        return [Workflow.model_validate(w) for w in items]

    def get_workflow(self, workflow_id: str) -> Workflow:
        """Get a workflow with its steps."""
        data = self._http.get(f"/v1/orchestrator/workflows/{workflow_id}")
        return Workflow.model_validate(data)

    def run_workflow(
        self,
        workflow_id: str,
        input: str = "",
        *,
        timeout: float = 300.0,
    ) -> WorkflowRun:
        """Execute a workflow.

        This runs the full agent pipeline: governance gate → memory
        retrieve → LLM inference → tool execution → decision trail.

        Args:
            workflow_id: Workflow to execute.
            input: The user input / prompt.
            timeout: Request timeout in seconds (default 300).

        Returns:
            A :class:`WorkflowRun` with status and step details.
        """
        body: dict[str, Any] = {"input_text": input} if input else {"input_text": ""}

        data = self._http.post(
            f"/v1/orchestrator/workflows/{workflow_id}/run",
            json=body,
            timeout=timeout,
        )
        return WorkflowRun.model_validate(data)

    def resume_workflow(
        self,
        workflow_id: str,
        approved: bool = True,
        *,
        feedback: str = "",
    ) -> WorkflowRun:
        """Resume a workflow after HITL escalation.

        Args:
            workflow_id: The workflow in ``WAITING_HITL`` status.
            approved: Whether the human approved continuation.
            feedback: Optional human feedback.

        Returns:
            A :class:`WorkflowRun` with the final status.
        """
        body: dict[str, Any] = {"approved": approved}
        if feedback:
            body["feedback"] = feedback

        data = self._http.post(
            f"/v1/orchestrator/workflows/{workflow_id}/resume",
            json=body,
        )
        return WorkflowRun.model_validate(data)

    def stream_workflow(
        self,
        workflow_id: str,
        input: str = "",
        *,
        timeout: float = 300.0,
    ) -> "Generator[StreamEvent, None, None]":
        """Stream a workflow execution via Server-Sent Events.

        Yields :class:`~grafomem.types.StreamEvent` objects in real-time
        as the workflow executes.  Each event corresponds to a stage in
        the governed execution pipeline (governance, memory, LLM, etc.).

        Args:
            workflow_id: Workflow to execute.
            input: The user input / prompt.
            timeout: Request timeout in seconds (default 300).

        Yields:
            :class:`~grafomem.types.StreamEvent` with ``event`` type and
            ``data`` payload.

        Example::

            for event in client.orchestrator.stream_workflow(wf_id, "Analyze Q2"):
                print(f"{event.event}: {event.data}")
                if event.event == "workflow.complete":
                    print(f"Done in {event.data['duration_ms']}ms")
        """
        import json as _json
        from typing import Generator

        body = {"input_text": input} if input else {"input_text": ""}
        resp = self._http.stream_post(
            f"/v1/orchestrator/workflows/{workflow_id}/stream",
            json=body,
            timeout=timeout,
        )

        try:
            event_type = None
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    event_type = line[7:].strip()
                elif line.startswith("data: ") and event_type:
                    try:
                        data = _json.loads(line[6:])
                    except _json.JSONDecodeError:
                        continue
                    yield StreamEvent(event=event_type, data=data)
                    event_type = None
        finally:
            resp.close()

    # ── Receipts & Chain Verification ─────────────────────────────────

    def receipts(self, workflow_id: str) -> list[Receipt]:
        """Get all execution receipts for a workflow.

        Returns:
            List of :class:`Receipt` forming the hash chain.
        """
        data = self._http.get(f"/v1/orchestrator/workflows/{workflow_id}/receipts")
        items = data if isinstance(data, list) else data.get("receipts", [])
        return [Receipt.model_validate(r) for r in items]

    def verify_chain(self, workflow_id: str) -> ChainVerification:
        """Verify the integrity of a workflow's receipt hash chain.

        Returns:
            A :class:`ChainVerification` with ``status`` (``"intact"``
            or ``"tampered"``).
        """
        data = self._http.get(
            f"/v1/orchestrator/workflows/{workflow_id}/verify-chain",
        )
        return ChainVerification.model_validate(data)

    # ── Replay ────────────────────────────────────────────────────────

    def replay(self, decision_id: str) -> ReplayResult:
        """Replay a decision to verify reproducibility.

        Args:
            decision_id: The decision record ID to replay.

        Returns:
            A :class:`ReplayResult` with ``status`` (``"identical"``
            or ``"diverged"``), ``confidence``, and both outputs.
        """
        data = self._http.post(f"/v1/orchestrator/replay/{decision_id}")
        return ReplayResult.model_validate(data)

    # ── Stats ─────────────────────────────────────────────────────────

    def stats(self) -> OrchestratorStats:
        """Get orchestrator summary statistics."""
        data = self._http.get("/v1/orchestrator/stats")
        return OrchestratorStats.model_validate(data)
