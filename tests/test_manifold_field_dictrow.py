"""Regression: /field row access must work with BOTH the cloud pool's psycopg3
dict_row cursors and the non-pool psycopg2 tuple cursors. The first deploy 500'd with
`KeyError: 0` because `cur.fetchone()[0]` indexed a dict_row by position — this locks
the dict/tuple-agnostic access + the explicit column aliases the endpoint now uses.
"""
from __future__ import annotations

import uuid

import psycopg
from psycopg.rows import dict_row

from aml.backends.vector_only import _stub_embedder
from aml.cloud.decision_trail import DecisionTrailService

URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def _cell(row, name, idx):
    # the exact helper the endpoint uses
    return row[name] if isinstance(row, dict) else row[idx]


def test_field_queries_work_dictrow_and_tuple():
    dt = DecisionTrailService(URL, embed_fn=_stub_embedder(384), tokenizer_id="stub-384")
    dt.ensure_schema()
    T = "mf-" + uuid.uuid4().hex[:8]
    rec = dt.log(tenant_id=T, store_id="governed", query="{}", model_id="m", raw_output="{}",
                 parameters={"decision": "certify", "verifiability_tag": "judgment",
                             "cgr_schema": "cgr.decision.v1", "invoice_ref": "OUT-x",
                             "agent_key": "k" * 64, "agent_handle": "a@vb"})
    did = rec.decision_id

    COUNT = "SELECT count(*) AS n FROM decision_embeddings WHERE tenant_id=%s AND valid_until IS NULL"
    SEL = ("SELECT decision_id, embedding::text AS emb FROM decision_embeddings "
           "WHERE tenant_id=%s AND valid_until IS NULL")

    # dict_row (the cloud RoutingPool path — this is what 500'd before the fix)
    with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as c:
        n = int(_cell(c.execute(COUNT, (T,)).fetchone(), "n", 0))
        assert n == 1
        r = c.execute(SEL, (T,)).fetchone()
        assert _cell(r, "decision_id", 0) == did
        vec = [float(x) for x in str(_cell(r, "emb", 1)).strip("[]").split(",")]
        assert len(vec) == 384

    # tuple rows (non-pool psycopg2-style fallback)
    with psycopg.connect(URL, autocommit=True) as c:
        n = int(_cell(c.execute(COUNT, (T,)).fetchone(), "n", 0))
        assert n == 1
        r = c.execute(SEL, (T,)).fetchone()
        assert _cell(r, "decision_id", 0) == did
