import pytest
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock
from aml.cloud.manifold import serialize_manifold, ManifoldService

def test_serialize_manifold_badge_counts():
    """Test that the provenance badge correctly computes real vs synthetic counts."""
    df = pd.DataFrame([
        {"step_id": "1", "is_synthetic": True, "_q": 0, "_r": 0, "governance_logs": [], "agent_role": "bot", "governance_allowed": True, "latency_ms": 10, "workflow_id": "wf1", "model_id": "model1", "created_at": "2024-01-01T00:00:00Z"},
        {"step_id": "2", "is_synthetic": True, "_q": 0, "_r": 0, "governance_logs": [], "agent_role": "bot", "governance_allowed": True, "latency_ms": 10, "workflow_id": "wf1", "model_id": "model1", "created_at": "2024-01-01T00:00:00Z"},
        {"step_id": "3", "is_synthetic": False, "_q": 0, "_r": 0, "governance_logs": [], "agent_role": "bot", "governance_allowed": True, "latency_ms": 10, "workflow_id": "wf1", "model_id": "model1", "created_at": "2024-01-01T00:00:00Z"}
    ])
    bmu = np.zeros((3, 2))
    payload = serialize_manifold(df, bmu, side=6)
    
    steps_prov = payload["provenance"]["steps"]
    assert steps_prov["synthetic_count"] == 2
    assert steps_prov["real_count"] == 1
    assert steps_prov["source"] == "mixed"

def test_serialize_manifold_lenses():
    """Test that cell lenses correctly aggregate the typed StepStatus values."""
    df = pd.DataFrame([
        {"step_id": "1", "status": "failed_timeout", "is_synthetic": False, "_q": 1, "_r": 1, "governance_logs": [], "agent_role": "bot", "governance_allowed": True, "latency_ms": 10, "workflow_id": "wf1", "model_id": "model1", "created_at": "2024-01-01T00:00:00Z"},
        {"step_id": "2", "status": "halted_loop", "is_synthetic": False, "_q": 1, "_r": 1, "governance_logs": [], "agent_role": "bot", "governance_allowed": True, "latency_ms": 10, "workflow_id": "wf1", "model_id": "model1", "created_at": "2024-01-01T00:00:00Z"},
        {"step_id": "3", "status": "failed_failover", "is_synthetic": False, "_q": 1, "_r": 1, "governance_logs": [], "agent_role": "bot", "governance_allowed": True, "latency_ms": 10, "workflow_id": "wf1", "model_id": "model1", "created_at": "2024-01-01T00:00:00Z"},
        {"step_id": "4", "status": "completed", "is_synthetic": False, "_q": 1, "_r": 1, "governance_logs": [], "agent_role": "bot", "governance_allowed": True, "latency_ms": 10, "workflow_id": "wf1", "model_id": "model1", "created_at": "2024-01-01T00:00:00Z"}
    ])
    bmu = np.ones((4, 2))
    payload = serialize_manifold(df, bmu, side=6)
    
    # All rows map to cell _q=1, _r=1, which is the only cell created (or first one)
    cell = payload["cells"][0]
    assert cell["lenses"]["timeout"] == 1
    assert cell["lenses"]["loop"] == 1
    assert cell["lenses"]["failover"] == 1
    assert cell["count"] == 4

def test_locate_step_bmu_routing():
    """Test that locate_step dynamically embeds and routes a step using cached MiniSom weights."""
    ms = ManifoldService("postgresql://fake")
    
    class DummyEmbedder:
        def encode(self, texts, **kwargs):
            return np.ones((len(texts), 384))
            
    ms._embedder = DummyEmbedder()
    
    # Fake db connection
    mock_db = MagicMock()
    ms._conn = mock_db
    
    # We will bypass the context manager or connection logic by patching read_sql and the cur execution
    df_step = pd.DataFrame([
        {
            "step_id": "step123", "input_text": "hello", "raw_output": "world", 
            "tool_calls": [], "retrieved_facts": [], "agent_role": "bot",
            "model_id": "mock", "latency_ms": 100, "governance_allowed": True,
            "is_synthetic": False, "status": "completed", "workflow_id": "wf1",
            "parent_decision_id": None, "tenant_id": "test_tenant",
            "created_at": "2024-01-01T00:00:00Z"
        }
    ])
    
    fake_features = np.zeros((1, 5))
    side = 6
    feature_dim = 5
    fake_weights = np.ones((side, side, feature_dim))
    
    payload = {
        "meta": {"somGrid": [side, side]}
    }
    
    with patch("aml.cloud.manifold.pd.read_sql", return_value=df_step), \
         patch("aml.cloud.manifold.build_features", return_value=fake_features), \
         patch("psycopg2.connect", return_value=mock_db):
        
        mock_cursor = MagicMock()
        # Return row for manifold_cache query: (payload, som_version, som_weights)
        mock_cursor.fetchone.return_value = (payload, "fake-som-version", fake_weights.tobytes())
        mock_db.cursor.return_value = mock_cursor
        
        res = ms.locate_step("step123", "test_tenant")
        
        assert res is not None
        assert "error" not in res
        assert res["stepId"] == "step123"
        assert res["somVersion"] == "fake-som-version"
        assert res["cellId"] is not None
        assert res["cellId"].startswith("c_")
