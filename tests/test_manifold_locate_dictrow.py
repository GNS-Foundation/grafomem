"""Regression: locate_step's manifold_cache read must work with the cloud pool's
psycopg3 dict_row cursors. It read row[1]/row[2] positionally → KeyError on dict_row
(the /field 500 class). This exercises a real dict_row read of manifold_cache + the
dict/tuple-agnostic accessor locate_step now uses.
"""
from __future__ import annotations

import json
import uuid

import psycopg
from psycopg.rows import dict_row

from aml.cloud.manifold import ManifoldService

URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def _cell(r, name, idx):
    return r[name] if isinstance(r, dict) else r[idx]


def test_manifold_cache_read_dictrow_and_tuple():
    ManifoldService(URL).ensure_schema()   # creates manifold_cache
    T = "loc-" + uuid.uuid4().hex[:8]
    payload = {"meta": {"somGrid": [6, 6]}}
    weights = b"\x00" * 32
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(
            "INSERT INTO manifold_cache (tenant_id, payload, updated_at, som_version, som_weights) "
            "VALUES (%s, %s, NOW(), %s, %s) ON CONFLICT (tenant_id) DO UPDATE SET "
            "payload=EXCLUDED.payload, som_version=EXCLUDED.som_version, som_weights=EXCLUDED.som_weights",
            (T, json.dumps(payload), "v1", weights),
        )

    SEL = "SELECT payload, som_version, som_weights FROM manifold_cache WHERE tenant_id=%s"

    # dict_row (cloud pool path — this is what 500'd in locate_step before the fix)
    with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as c:
        row = c.execute(SEL, (T,)).fetchone()
        assert row and _cell(row, "som_version", 1) == "v1"          # would KeyError:1 before
        assert bytes(_cell(row, "som_weights", 2)) == weights
        pl = _cell(row, "payload", 0)
        pl = pl if isinstance(pl, dict) else json.loads(pl)
        assert pl["meta"]["somGrid"][0] == 6

    # tuple rows (non-pool psycopg2-style fallback)
    with psycopg.connect(URL, autocommit=True) as c:
        row = c.execute(SEL, (T,)).fetchone()
        assert _cell(row, "som_version", 1) == "v1"
        assert bytes(_cell(row, "som_weights", 2)) == weights
