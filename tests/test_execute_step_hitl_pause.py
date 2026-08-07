"""Phase 2, F3 (remainder) — the execute_step PAUSE, proven end-to-end.

Drives the REAL OrchestratorService.execute_step with a mock LLM that emits a send_email
proposal to a named human and a governance gate that escalates send_email. Asserts the
tenant-visible behavior of PR-4:
  (1) workflow status = WAITING_HITL (not COMPLETED); step status = ESCALATED,
  (2) the tool registry's execute() was NEVER called (send-less — nothing sent),
  (3) an HITL request was created and the step paused at the break.

DB-gated: uses local Postgres for the real persistence (create_agent/workflow, _persist_step,
_create_hitl_request, _update_workflow_status). SKIPS cleanly when PG is unreachable — this is
the staging integration test; it must never silently pass.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

_TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


# ── Mocks: the LLM proposes a send; governance escalates it; the tool is a spy ──

class _Resp:
    content = "I propose sending an outreach email."
    tokens_output = 12
    tokens_input = 12
    model_id = "mock"
    latency_ms = 1
    raw_response: dict = {}

    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


class _FakeLLM:
    def infer(self, tenant_id, request):
        return _Resp([{"name": "send_email",
                       "arguments": {"to": "Ana Ruiz", "subject": "intro",
                                     "invoice_ref": "OUT-globex-ana"}}])


class _FakeToolReg:
    def __init__(self):
        self.execute_calls = []

    def get_tool_definitions(self, tenant_id, tools):
        return [{"name": "send_email", "description": "send", "input_schema": {}}]

    def execute(self, tenant_id, name, args):
        self.execute_calls.append((tenant_id, name, args))   # must stay EMPTY
        return None


class _FakeGov:
    """Allows inference; ESCALATES the send_email tool call."""

    def evaluate_and_gate(self, tenant_id, operation, context):
        from aml.cloud.governance import EvaluationResult

        class _EscLog:
            result = EvaluationResult.ESCALATED
        if operation == "tool_execution" and context.get("tool_name") == "send_email":
            return (False, [_EscLog()])
        return (True, [])

    def evaluate(self, tenant_id, operation, context):
        return []

    def redact(self, tenant_id, text):
        return text

    @staticmethod
    def log_to_dict(log):
        return {"result": getattr(getattr(log, "result", None), "value", "escalated")}


class _Rec:
    decision_id = "dec-1"
    signature = None
    public_key = None
    tenant_id = "t"
    model_id = "mock"
    raw_output = ""
    created_at = datetime(2026, 8, 6, tzinfo=timezone.utc)


class _FakeTrail:
    def log(self, **kw):
        return _Rec()


def _orch_or_skip():
    try:
        import psycopg  # noqa: F401
        from aml.cloud.migrations_runner import apply_migrations
        from aml.cloud.orchestrator import OrchestratorService
        apply_migrations(_TEST_DB_URL)
        orch = OrchestratorService(
            _TEST_DB_URL, governance=_FakeGov(), decision_trail=_FakeTrail(),
            store_manager=None, llm_registry=_FakeLLM(), tool_registry=_FakeToolReg(),
            signing_identity=None,
        )
        orch.ensure_schema()
        return orch
    except Exception as e:
        pytest.skip(f"local Postgres not reachable ({e}); F3 pause test must run on staging.")


def test_send_email_escalation_pauses_execute_step():
    orch = _orch_or_skip()
    try:
        from aml.cloud.orchestrator import WorkflowStatus, StepStatus, WorkflowMode

        tenant = f"f3-{uuid.uuid4().hex[:8]}"
        agent = orch.create_agent(
            tenant, name="gtm-outreach-agent@ulissy", role="custom", model_id="mock",
            system_prompt="Propose outreach; sends require approval.",
            tools=["send_email"], agent_key="a" * 64,
        )
        wf = orch.create_workflow(tenant, name="outreach", agent_ids=[agent.agent_id],
                                  mode=WorkflowMode.SEQUENTIAL)

        step = orch.execute_step(wf.workflow_id, agent.agent_id, "Propose outreach to Ana Ruiz")

        # (1) paused, not completed
        assert step.status == StepStatus.ESCALATED, f"step status={step.status}"
        assert orch.get_workflow(wf.workflow_id).status == WorkflowStatus.WAITING_HITL

        # (2) the send-less guarantee: the tool was NEVER executed
        assert orch._tool_registry.execute_calls == [], "send_email must NOT execute before approval"

        # (3) an HITL request was created and linked to the paused step
        assert step.hitl_request_id, "escalated step must carry an HITL request id"
        import psycopg
        with psycopg.connect(_TEST_DB_URL) as conn:
            rows = conn.execute(
                "SELECT action, resource, status FROM hitl_approval_requests "
                "WHERE workflow_id = %s AND status = 'pending'",
                (wf.workflow_id,),
            ).fetchall()
        assert len(rows) == 1, f"expected 1 pending HITL request, got {len(rows)}"
        assert rows[0][0] == "send_email"              # queue shows the tool
        assert rows[0][1] == "Ana Ruiz"                # ...and the recipient
    finally:
        orch.close()
