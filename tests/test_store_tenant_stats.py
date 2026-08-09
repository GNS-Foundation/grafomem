"""1B-2 — per-tenant fact rollup for GET /v1/stores.

`PostgresGMPBackend.tenant_stats` + `StoreManager.tenant_stats` return the tenant's
current fact_count + stored bytes. Isolation here rests on the explicit `WHERE
tenant_id = %s` (load-bearing under today's owner/BYPASSRLS role, exactly like
scoped_audit) — so these assertions hold regardless of whether the DB role can be
RLS-constrained. Bytes use octet_length(coalesce(content_enc, content)); 'live' =
superseded_by IS NULL.
"""
from __future__ import annotations

import uuid

import pytest

from aml.backends.interface import WriteOptions
from aml.backends.postgres_gmp import PostgresGMPBackend
from aml.server.stores import StoreManager

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def _tenant() -> str:
    return f"stats-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def store():
    sm = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL))
    b = sm.get_or_create_named("cgr-outcomes").backend
    try:
        yield sm, b
    finally:
        b.close()


def _write(backend, tenant_id: str, content: str) -> int:
    return backend.write(content, WriteOptions(tenant_id=tenant_id))


def test_empty_tenant_is_zero(store):
    _sm, b = store
    assert b.tenant_stats(_tenant()) == {"fact_count": 0, "total_bytes": 0}


def test_count_and_bytes_plaintext(store):
    _sm, b = store
    t = _tenant()
    contents = ["alpha", "bravo-2", "charlie-333"]        # ascii → octet_length == len
    for c in contents:
        _write(b, t, c)
    stats = b.tenant_stats(t)
    assert stats["fact_count"] == len(contents)
    assert stats["total_bytes"] == sum(len(c) for c in contents)


def test_isolation_between_tenants(store):
    _sm, b = store
    a, other = _tenant(), _tenant()
    for c in ("a1", "a2", "a3"):
        _write(b, a, c)
    for c in ("b1", "b2"):
        _write(b, other, c)
    assert b.tenant_stats(a)["fact_count"] == 3
    assert b.tenant_stats(other)["fact_count"] == 2          # a's writes never leak in


def test_superseded_rows_excluded(store):
    _sm, b = store
    t = _tenant()
    ref = _write(b, t, "original")
    before = b.tenant_stats(t)["fact_count"]
    b.supersede(ref, "revised-content", None, WriteOptions(tenant_id=t))
    stats = b.tenant_stats(t)
    # supersede closes the old row (superseded_by set) and writes a new current one →
    # the count of *live* rows is unchanged, not inflated by the history row.
    assert stats["fact_count"] == before
    assert stats["total_bytes"] == len("revised-content")


def test_manager_rollup_matches_backend(store):
    sm, b = store
    t = _tenant()
    for c in ("x", "yy", "zzz"):
        _write(b, t, c)
    assert sm.tenant_stats(t) == b.tenant_stats(t)


def test_manager_returns_none_without_capable_backend():
    """A backend lacking tenant_stats (e.g. sqlite dev) yields None, not an error."""
    class _Dumb:
        pass
    sm = StoreManager(lambda: _Dumb())
    sm.get_or_create_named("dev")
    assert sm.tenant_stats(_tenant()) is None
