"""Phase 2, PR-0 — orchestrator propose_action records a CGR-attributed governed decision.

Pure/DB-free: a fake decision_trail captures the log() call; get_agent is monkeypatched.
Asserts the recorded parameters carry the CGR identity (agent_key/agent_handle/invoice_ref/
cgr_schema) so the CGR engine will group + join the decision.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aml.cloud.orchestrator import AgentDefinition, OrchestratorService

KEY = "a" * 64
_NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


class _FakeRec:
    decision_id = "dec-123"
    tenant_id = "corp"
    created_at = _NOW


class _FakeTrail:
    def __init__(self):
        self.calls = []

    def log(self, **kw):
        self.calls.append(kw)
        return _FakeRec()


def _agent(agent_key=KEY):
    return AgentDefinition(
        agent_id="aid", tenant_id="corp", name="gtm-outreach-agent@ulissy", role="custom",
        description="", model_id="claude-3-opus-20240229", fallback_models=[],
        system_prompt="", memory_stores=[], tools=[], max_steps=20, max_tokens_per_step=4096,
        temperature=0.7, enabled=True, created_at=_NOW, updated_at=_NOW,
        agent_key=agent_key, agent_handle="gtm-outreach-agent@ulissy",
    )


def _orch(trail):
    # __init__ only stores refs — no DB connection made.
    return OrchestratorService(db_url="", governance=None, decision_trail=trail, signing_identity=None)


def test_propose_action_records_cgr_attributed_decision(monkeypatch):
    trail = _FakeTrail()
    orch = _orch(trail)
    monkeypatch.setattr(orch, "get_agent", lambda aid, encryption=None: _agent())

    out = orch.propose_action("corp", "aid", "send_email",
                              {"to": "ana@globex.example", "subject": "hi"}, "OUT-globex-ana",
                              reason="fit: allocator")
    # return shape
    assert out["decision_id"] == "dec-123"
    assert out["invoice_ref"] == "OUT-globex-ana"
    assert out["agent_handle"] == "gtm-outreach-agent@ulissy"
    assert out["decision"] == "certify" and out["proposed"] is True and out["executed"] is False

    # exactly one governed decision recorded, with the CGR parameter shape
    assert len(trail.calls) == 1
    call = trail.calls[0]
    assert call["store_id"] == "governed"
    p = call["parameters"]
    assert p["agent_key"] == KEY
    assert p["agent_handle"] == "gtm-outreach-agent@ulissy"
    assert p["invoice_ref"] == "OUT-globex-ana" and p["invoice_id"] == "OUT-globex-ana"
    assert p["cgr_schema"] == "cgr.decision.v1"
    assert p["decision"] == "certify" and p["verifiability_tag"] == "judgment"
    assert p["tool"] == "send_email"


def test_propose_action_requires_agent_key(monkeypatch):
    orch = _orch(_FakeTrail())
    monkeypatch.setattr(orch, "get_agent", lambda aid, encryption=None: _agent(agent_key=None))
    with pytest.raises(ValueError):
        orch.propose_action("corp", "aid", "send_email", {}, "OUT-x")


def test_propose_action_missing_agent(monkeypatch):
    orch = _orch(_FakeTrail())
    monkeypatch.setattr(orch, "get_agent", lambda aid, encryption=None: None)
    with pytest.raises(KeyError):
        orch.propose_action("corp", "aid", "send_email", {}, "OUT-x")
