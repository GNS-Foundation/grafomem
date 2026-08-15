"""WriteOptions.skip_embedding (F1 completion) — fact-shaped CGR writes skip the
BGE embed + the memory_embeddings row, so a large review/outcome bulk no longer pays
per-write embedding cost. The row stays in `memories` (visible to scoped_audit /
scoring) but is absent from vector `retrieve` (correct — CGR substrate is never
semantically retrieved)."""
import os
import uuid

import pytest


def _backend():
    db = os.environ.get("GRAFOMEM_DB_URL")
    if not db:
        pytest.skip("GRAFOMEM_DB_URL not set")
    from aml.backends.postgres_gmp import PostgresGMPBackend
    return PostgresGMPBackend(db)


def _emb_count(backend, tenant_id):
    with backend._tenant_conn(tenant_id) as (conn, cur):
        cur.execute("SELECT COUNT(*) FROM memory_embeddings WHERE tenant_id = %s", (tenant_id,))
        return cur.fetchone()[0]


def test_skip_embedding_writes_no_embedding_row_but_stays_scan_visible():
    from aml.backends.interface import WriteOptions
    from aml.cgr.substrate import _scoped_audit

    b = _backend()
    T = f"skipemb-{uuid.uuid4().hex[:8]}"

    b.write("normal | fact", WriteOptions(tenant_id=T, metadata={"k": "normal"}))
    b.write("skipped | fact", WriteOptions(tenant_id=T, metadata={"k": "skipped"}, skip_embedding=True))

    # both rows land in `memories` (scan-visible → scoring/CGR unaffected)
    scanned = {(m.metadata or {}).get("k") for m in _scoped_audit(b, T)}
    assert {"normal", "skipped"} <= scanned

    # only the NON-skipped write created a vector row
    assert _emb_count(b, T) == 1


def test_skip_embedding_row_absent_from_vector_retrieve():
    from aml.backends.interface import RetrieveOptions, WriteOptions

    b = _backend()
    T = f"skipemb-{uuid.uuid4().hex[:8]}"
    b.write("alpha content about receivables", WriteOptions(tenant_id=T, metadata={"k": "kept"}))
    b.write("beta content about receivables", WriteOptions(tenant_id=T, metadata={"k": "hidden"}, skip_embedding=True))

    got = {(m.metadata or {}).get("k") for m in b.retrieve("receivables", RetrieveOptions(tenant_id=T))}
    assert "kept" in got            # embedded row is retrievable
    assert "hidden" not in got      # skip_embedding row never enters vector search
