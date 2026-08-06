"""Phase 2, PR-2 + PR-3 — send_email stub tool + named-tool HITL policy.

DB-free: the handler and the policy evaluator are pure, exercised via __new__ instances.
"""
from __future__ import annotations

from aml.cloud.tool_registry import BUILTIN_TOOLS, ToolRegistry, ToolType
from aml.cloud.policy_engine import EvaluationResult, PolicyEngine


class _Policy:
    def __init__(self, config):
        self.config = config


# ---------------------------------------------------------------------------
# PR-2 — send_email is an interim, send-LESS stub
# ---------------------------------------------------------------------------

def test_email_send_tooltype_and_builtin_present():
    assert ToolType.EMAIL_SEND.value == "email_send"
    names = [t["name"] for t in BUILTIN_TOOLS]
    assert "send_email" in names
    se = next(t for t in BUILTIN_TOOLS if t["name"] == "send_email")
    assert se["tool_type"] == ToolType.EMAIL_SEND
    assert se["requires_governance"] is True
    assert "to" in se["input_schema"]["required"]


def test_exec_email_send_never_sends():
    tr = ToolRegistry.__new__(ToolRegistry)  # bypass __init__/DB
    out = tr._exec_email_send("corp", None, {"to": "ana@globex.example", "subject": "hi"})
    assert out["status"] == "approved_to_send"
    assert out["sent"] is False               # the load-bearing guarantee: no send
    assert out["to"] == "ana@globex.example"


# ---------------------------------------------------------------------------
# PR-3 — hitl_required policy can gate a NAMED tool
# ---------------------------------------------------------------------------

def _eval(cfg, operation, context):
    pe = PolicyEngine.__new__(PolicyEngine)
    return pe._eval_hitl(_Policy(cfg), operation, context)[0]


def test_hitl_policy_gates_named_tool():
    cfg = {"operations": ["tool_execution"], "tools": ["send_email"]}
    # send_email → escalate
    assert _eval(cfg, "tool_execution", {"tool_name": "send_email"}) == EvaluationResult.ESCALATED
    assert _eval(cfg, "tool_execution", {"tool": "send_email"}) == EvaluationResult.ESCALATED
    # a different tool → allowed
    assert _eval(cfg, "tool_execution", {"tool_name": "grafomem_write"}) == EvaluationResult.ALLOWED


def test_hitl_policy_backward_compatible_operation_level():
    cfg = {"operations": ["inference"]}                       # no `tools` filter
    assert _eval(cfg, "inference", {}) == EvaluationResult.ESCALATED
    assert _eval(cfg, "tool_execution", {}) == EvaluationResult.ALLOWED
    # empty operations = catch-all (existing semantics)
    assert _eval({}, "anything", {}) == EvaluationResult.ESCALATED
