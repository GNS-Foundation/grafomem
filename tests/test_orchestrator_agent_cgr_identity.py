"""Phase 2, PR-1 — orchestrated agents carry a stable CGR identity (agent_key/agent_handle).

Pure mapping tests always run. The create→read integration test skips cleanly when local
Postgres isn't reachable (it must never silently pass).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from aml.cloud.orchestrator import AgentDefinition, OrchestratorService
from aml.cloud.orchestrator_routes import CreateAgentRequest

_NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


def _row(**over) -> dict:
    base = {
        "agent_id": "aid", "tenant_id": "t1", "name": "gtm-outreach-agent@ulissy",
        "role": "custom", "description": "", "model_id": "claude-3-opus-20240229",
        "fallback_models": [], "system_prompt": "hi", "memory_stores": [], "tools": [],
        "max_steps": 20, "max_tokens": 4096, "temperature": 0.7, "enabled": True,
        "created_at": _NOW, "updated_at": _NOW,
        "agent_key": "a" * 64, "agent_handle": "gtm-outreach-agent@ulissy",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Pure mapping (no DB)
# ---------------------------------------------------------------------------

def test_row_to_agent_maps_cgr_identity():
    a = OrchestratorService._row_to_agent(_row())
    assert a.agent_key == "a" * 64
    assert a.agent_handle == "gtm-outreach-agent@ulissy"


def test_row_to_agent_tolerates_missing_identity():
    # legacy rows (pre-migration) have no agent_key/agent_handle columns
    r = _row()
    del r["agent_key"]; del r["agent_handle"]
    a = OrchestratorService._row_to_agent(r)
    assert a.agent_key is None and a.agent_handle is None


def test_agent_to_dict_includes_cgr_identity():
    a = OrchestratorService._row_to_agent(_row())
    d = OrchestratorService.agent_to_dict(a)
    assert d["agent_key"] == "a" * 64
    assert d["agent_handle"] == "gtm-outreach-agent@ulissy"


def test_row_to_dict_round_trip_preserves_identity():
    a = OrchestratorService._row_to_agent(_row(agent_key="b" * 64, agent_handle="finance-agent@ulissy"))
    d = OrchestratorService.agent_to_dict(a)
    assert (d["agent_key"], d["agent_handle"]) == ("b" * 64, "finance-agent@ulissy")


def test_agent_definition_defaults_identity_optional():
    # positional construction without the new fields still works (defaults to None)
    a = AgentDefinition(
        agent_id="x", tenant_id="t", name="n", role="custom", description="",
        model_id="m", fallback_models=[], system_prompt="", memory_stores=[], tools=[],
        max_steps=1, max_tokens_per_step=1, temperature=0.0, enabled=True,
        created_at=_NOW, updated_at=_NOW,
    )
    assert a.agent_key is None and a.agent_handle is None


def test_create_agent_request_identity_fields():
    # optional, default None; accepted when supplied
    assert CreateAgentRequest(name="n", model_id="m", system_prompt="s").agent_key is None
    r = CreateAgentRequest(name="n", model_id="m", system_prompt="s",
                           agent_key="c" * 64, agent_handle="code-agent@ulissy")
    assert r.agent_key == "c" * 64 and r.agent_handle == "code-agent@ulissy"


# ---------------------------------------------------------------------------
# create → read through the DB (SKIPS without local Postgres)
# ---------------------------------------------------------------------------

_TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def test_create_agent_persists_and_reads_back_identity():
    try:
        import psycopg  # noqa: F401
        from aml.cloud.migrations_runner import apply_migrations
        apply_migrations(_TEST_DB_URL)                         # exercises 007_*.sql
        orch = OrchestratorService(_TEST_DB_URL, governance=None, decision_trail=None)
        orch.ensure_schema()
    except Exception as e:
        pytest.skip(f"local Postgres not reachable ({e}); PR-1 DB round-trip must run on staging.")
    try:
        key = uuid.uuid4().hex * 2  # 64 hex
        agent = orch.create_agent(
            tenant_id=f"pr1-{uuid.uuid4().hex[:8]}",
            name="gtm-outreach-agent@ulissy", role="custom",
            model_id="claude-3-opus-20240229", system_prompt="propose outreach",
            agent_key=key,
        )
        assert agent.agent_key == key
        assert agent.agent_handle == "gtm-outreach-agent@ulissy"  # defaulted from name
        got = orch.get_agent(agent.agent_id)
        assert got is not None
        assert got.agent_key == key and got.agent_handle == "gtm-outreach-agent@ulissy"
    finally:
        orch.close()
