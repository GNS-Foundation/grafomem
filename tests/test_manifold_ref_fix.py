"""1B-1 — manifold fact-vector fix.

Two stacked bugs meant real fact embeddings never loaded: the query hit a
non-existent `fact_ref` column, and `retrieved_facts` elements are dicts
{ref:int, ...} that the old `isinstance(r, str)` filter dropped. These tests lock
the dict/int-ref path and the honest `source`/`matched` provenance. (The end-to-end
DB join is the live acceptance check: matched>0 on virtualbank.)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from aml.cloud.manifold import EMB_DIM, make_about_vectors, serialize_manifold


class _StubEmbedder:
    """Deterministic zero text-embeddings so a blended fact-vector is detectable."""
    def encode(self, texts, normalize_embeddings=True):
        return np.zeros((len(texts), EMB_DIM))


def test_make_about_vectors_resolves_int_ref_dicts():
    # retrieved_facts is a list of dicts keyed by int `ref` (the real wire shape).
    df = pd.DataFrame([{
        "input_text": "q", "raw_output": "a",
        "retrieved_facts": [{"ref": 42, "content": "x"}],
        "retrieval_scores": [1.0],
    }])
    fact_vec = np.ones(EMB_DIM)

    with_fact = make_about_vectors(df, {42: fact_vec}, _StubEmbedder())
    text_only = make_about_vectors(df, {}, _StubEmbedder())

    # With a matching int-ref embedding the row vector blends the fact vector, so it
    # must differ from the text-only vector. The OLD isinstance(str) filter dropped
    # the dict → identical → this asserts the fix.
    assert not np.allclose(with_fact, text_only)
    assert not np.allclose(with_fact[0], 0.0)   # real content, not a zero vector


def test_make_about_vectors_falls_back_when_ref_absent():
    df = pd.DataFrame([{
        "input_text": "q", "raw_output": "a",
        "retrieved_facts": [{"ref": 99, "content": "x"}],
        "retrieval_scores": [1.0],
    }])
    # ref 99 not in lookup → text-only path, no crash on the dict.
    out = make_about_vectors(df, {42: np.ones(EMB_DIM)}, _StubEmbedder())
    assert out.shape == (1, EMB_DIM)


def _one_real_row():
    return pd.DataFrame([{
        "step_id": "1", "is_synthetic": False, "governance_logs": [], "agent_role": "bot",
        "governance_allowed": True, "latency_ms": 10, "workflow_id": "wf1", "model_id": "m1",
        "created_at": "2024-01-01T00:00:00Z",
    }])


def test_vectors_provenance_stamps_source_and_matched():
    payload = serialize_manifold(_one_real_row(), np.zeros((1, 2)), side=6,
                                 source="real-vectors", vectors_matched=3, vectors_requested=5)
    v = payload["provenance"]["vectors"]
    assert v["source"] == "real-vectors"
    assert v["matched"] == 3 and v["requested"] == 5
    assert payload["meta"]["source"] == "real-vectors"


def test_vectors_source_does_not_touch_steps_source():
    # steps.source is the FE-consumed field — must stay computed from is_synthetic,
    # independent of the vectors source string.
    payload = serialize_manifold(_one_real_row(), np.zeros((1, 2)), side=6,
                                 source="text-only", vectors_matched=0, vectors_requested=4)
    assert payload["provenance"]["vectors"]["source"] == "text-only"
    assert payload["provenance"]["steps"]["source"] == "real"   # all is_synthetic=False


def test_matched_defaults_zero_for_legacy_callers():
    payload = serialize_manifold(_one_real_row(), np.zeros((1, 2)), side=6)
    v = payload["provenance"]["vectors"]
    assert v["matched"] == 0 and v["requested"] == 0
