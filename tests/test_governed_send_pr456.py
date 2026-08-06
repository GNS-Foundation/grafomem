"""Phase 2, PR-5 + PR-6 — HITL request commits to the concrete action; approve executes it.

DB-free: _create_hitl_request runs against a fake connection; execute_approved_action runs
against a fake tool registry. (PR-4's execute_step pause coordination is integration-level —
verified on staging, flagged for review.)
"""
from __future__ import annotations

import json

from aml.cloud.orchestrator import OrchestratorService


def _orch():
    return OrchestratorService(db_url="", governance=None, decision_trail=None)


class _FakeConn:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


# ---------------------------------------------------------------------------
# PR-5 — the HITL request commits to {tool, recipient, invoice_ref}
# ---------------------------------------------------------------------------

def test_create_hitl_request_commits_proposed_action(monkeypatch):
    orch = _orch()
    fc = _FakeConn()
    monkeypatch.setattr(orch, "_get_conn", lambda: fc)

    rid = orch._create_hitl_request(
        "wf-1", tenant_id="corp", step_id="s1", agent_id="a1",
        proposed_action={"tool": "send_email", "to": "ana@globex.example",
                         "args": {"to": "ana@globex.example", "subject": "hi"},
                         "invoice_ref": "OUT-globex-ana"},
    )
    assert rid
    sql, params = fc.executed[0]
    # INSERT params: (request_id, tenant_id, workflow_id, step_id, action, resource, json, bytes, nonce, expires)
    action, resource, ctx_json = params[4], params[5], params[6]
    assert action == "send_email"                       # queue shows the tool
    assert resource == "ana@globex.example"             # ...and the recipient
    ctx = json.loads(ctx_json)
    assert ctx["proposed_action"]["tool"] == "send_email"
    assert ctx["proposed_action"]["invoice_ref"] == "OUT-globex-ana"
    # the signed bytes (context_bytes, index 7) commit to the action → approver signs the exact send
    assert b"send_email" in params[7]


def test_create_hitl_request_legacy_step_unchanged(monkeypatch):
    orch = _orch()
    fc = _FakeConn()
    monkeypatch.setattr(orch, "_get_conn", lambda: fc)
    orch._create_hitl_request("wf-1", tenant_id="corp", step_id="s1", agent_id="a1")
    _, params = fc.executed[0]
    assert params[4] == "execute_step" and params[5] == "a1"   # legacy behavior preserved


# ---------------------------------------------------------------------------
# PR-6 — approve executes the committed action deterministically + completes workflow
# ---------------------------------------------------------------------------

class _FakeResult:
    success = True
    output = {"status": "approved_to_send", "sent": False}


class _FakeTR:
    def __init__(self):
        self.calls = []

    def execute(self, tenant, tool, args):
        self.calls.append((tenant, tool, args))
        return _FakeResult()


def test_execute_approved_action_runs_committed_tool(monkeypatch):
    orch = _orch()
    orch._tool_registry = _FakeTR()
    done = []
    monkeypatch.setattr(orch, "_update_workflow_status", lambda wf, st, **k: done.append(("status", wf)))
    monkeypatch.setattr(orch, "_set_workflow_completed", lambda wf: done.append(("completed", wf)))

    out = orch.execute_approved_action(
        "corp", "wf-1",
        {"tool": "send_email", "args": {"to": "ana@globex.example"}, "invoice_ref": "OUT-globex-ana"},
    )
    assert out["executed"] is True
    assert out["invoice_ref"] == "OUT-globex-ana"
    assert orch._tool_registry.calls == [("corp", "send_email", {"to": "ana@globex.example"})]
    assert ("completed", "wf-1") in done       # workflow completed deterministically (no LLM re-run)
