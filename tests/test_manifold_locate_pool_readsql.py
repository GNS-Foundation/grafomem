"""Regression: locate_step's *data load* (step 2) over the cloud RoutingPool.

The cloud pool yields psycopg3 dict_row cursors. locate_step used `pd.read_sql` for
BOTH the pooled and non-pooled paths — and pd.read_sql over a dict_row connection
mangles the frame so the header column names leak into the data, surfacing downstream
as `could not convert string to float: 'tokens_used'` in build_features. That was the
real /locate 500 (it dies at step 2, before the manifold_cache read at step 3 that the
earlier _cell fix addressed).

The fix mirrors _compute_manifold_sync: when pooled, fetch dict rows explicitly and
build the DataFrame from them; only the local psycopg2 fallback uses pd.read_sql.

This test drives the POOL branch with a fake dict_row pool and asserts:
  1. pd.read_sql is NEVER called on the pool path (revert-guard),
  2. the DataFrame handed to build_features keeps a NUMERIC tokens_used column
     (the exact thing the bug destroyed),
  3. locate_step returns a real cellId, not an error.
The non-pool (pd.read_sql) branch stays covered by test_phase3_manifold.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from aml.cloud.manifold import ManifoldService


def _step_dict_rows():
    # One EXTRACTION_SQL row as a psycopg3 dict_row would deliver it: real dict, numeric
    # numeric columns. retrieved_facts empty ⇒ the memory_embeddings lookup is skipped.
    return [{
        "step_id": "s1", "agent_role": "bot", "workflow_id": "wf1", "model_id": "mock",
        "governance_allowed": True, "tool_calls": [], "governance_logs": [],
        "retrieved_facts": [], "tokens_used": 123, "latency_ms": 45, "step_number": 2,
        "created_at": "2024-01-01T00:00:00Z", "input_text": "hello", "raw_output": "world",
        "parent_decision_id": None, "is_synthetic": False, "status": "completed",
    }]


def test_locate_step_pool_path_builds_numeric_frame_without_readsql():
    ms = ManifoldService("postgresql://fake")

    class DummyEmbedder:
        def encode(self, texts, **kwargs):
            return np.ones((len(texts), 384))

    ms._embedder = DummyEmbedder()

    payload = {"meta": {"somGrid": [6, 6]}}
    weights = np.ones((6, 6, 5)).tobytes()

    # A cursor that is BOTH a context manager (step load) and a plain cursor (manifold_cache).
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchall.return_value = _step_dict_rows()          # step load (pool branch)
    cur.fetchone.return_value = (payload, "v-pool", weights)  # manifold_cache read
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = cur

    class FakePool:
        def getconn(self):
            return fake_conn

        def putconn(self, c):
            pass

    ms.pool = FakePool()

    captured = {}

    def _capture_build_features(df, about):
        captured["df"] = df
        return np.zeros((1, 5))

    def _no_read_sql(*a, **k):
        raise AssertionError("pool path must not call pd.read_sql (dict_row mangles the frame)")

    with patch("aml.cloud.manifold.pd.read_sql", side_effect=_no_read_sql), \
         patch("aml.cloud.manifold.build_features", side_effect=_capture_build_features), \
         patch("aml.cloud.manifold.apply_tenant_context"):
        res = ms.locate_step("s1", "T")

    assert "error" not in res, res
    assert res["stepId"] == "s1"
    assert res["somVersion"] == "v-pool"
    assert res["cellId"].startswith("c_")
    # The DataFrame built from dict rows kept numeric dtypes — the bug turned tokens_used
    # into the literal string 'tokens_used' (object dtype) → the 500.
    assert pd.api.types.is_numeric_dtype(captured["df"]["tokens_used"])
    assert int(captured["df"]["tokens_used"].iloc[0]) == 123
