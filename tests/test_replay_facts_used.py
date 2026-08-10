"""Wave-2 — replay `facts_used` is built from the record, independent of the store.

Regression: facts_used was nested inside the store-audit block, so a decision whose
store isn't registered in this process returned facts_used=[] even though it had
retrieved facts. These tests log a decision against an UNREGISTERED store and assert
facts_used still populates (memory_state, which genuinely needs the store, stays empty).
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from aml.server.app import create_app
from aml.cloud.tenant_manager import TenantManager

DB = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


@pytest.fixture(scope="module")
def client_key():
    os.environ["GRAFOMEM_DB_URL"] = DB
    os.environ["AUTH_MODE"] = "cloud"
    os.environ["GRAFOMEM_DB_POOL_MAX"] = "20"
    os.environ["GRAFOMEM_SIGNING_KEY"] = "b" * 64
    tm = TenantManager(DB)
    tm.ensure_schema()
    info = tm.create_tenant(name=f"replay-{uuid.uuid4().hex[:8]}")
    app = create_app(db_url=DB)
    with TestClient(app) as c:
        yield c, info.api_key


def _log(c, key, **over):
    body = {
        "store_id": f"never-registered-{uuid.uuid4().hex[:8]}",
        "query": "q", "model_id": "m", "raw_output": "o",
        "retrieved_fact_refs": [1130, 1140, 1135],
        "retrieved_contents": ["alpha", "bravo", "charlie"],
        "retrieval_scores": [0.9, 0.8, 0.7],
        **over,
    }
    r = c.post("/v1/decisions/log", json=body, headers={"X-API-Key": key})
    assert r.status_code in (200, 201), r.text
    return r.json()["decision_id"]


def test_facts_used_populates_without_registered_store(client_key):
    c, key = client_key
    did = _log(c, key)
    r = c.get(f"/v1/decisions/{did}/replay", headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    data = r.json()
    fu = data["facts_used"]
    assert len(fu) == 3                                   # the fix: populated despite no store
    assert fu[0]["ref"] == 1130 and fu[0]["content"] == "alpha" and fu[0]["score"] == 0.9
    # memory_state genuinely needs the store → empty for an unregistered store
    assert data["memory_state_at_decision"] == []


def test_facts_used_scores_padded_not_truncated(client_key):
    c, key = client_key
    did = _log(c, key, retrieved_fact_refs=[7, 8], retrieved_contents=["x", "y"], retrieval_scores=[])
    fu = c.get(f"/v1/decisions/{did}/replay", headers={"X-API-Key": key}).json()["facts_used"]
    assert len(fu) == 2                                   # NOT zip-truncated by empty scores
    assert fu[0]["ref"] == 7 and fu[0]["content"] == "x" and fu[0]["score"] is None
