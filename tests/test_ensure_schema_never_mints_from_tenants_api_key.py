"""Regression guard for 014: ensure_schema must NEVER mint a tenant_api_keys row from
tenants.api_key.

014 step (a) removed the ensure_schema backfill that copied tenants.api_key into
tenant_api_keys — because that made tenants.api_key a credential source, the exact path 014
closes. This test is the standing guard that the backfill stays gone (it replaces the
transitional guard test deleted in the #164 merge, which asserted the *opposite* — that the
now-removed backfill ran).

A tenant with a populated tenants.api_key and NO tenant_api_keys row must, after
ensure_schema(), still have ZERO tenant_api_keys rows: nothing may resurrect the column as a
minting source.

- Passes on main (backfill removed).
- Fails on c5de104 (#165's guarded backfill) — a non-NULL tenants.api_key matches
  `WHERE api_key IS NOT NULL` and gets minted, so the count is 1, not 0.
"""
import os
import uuid

import psycopg
import pytest

from aml.cloud.tenant_manager import TenantManager

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


@pytest.fixture
def fresh_tables():
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS tenant_api_keys CASCADE")
        c.execute("DROP TABLE IF EXISTS tenants CASCADE")
    yield


def test_ensure_schema_never_mints_from_tenants_api_key(fresh_tables):
    # 1. Create the tables.
    TenantManager(DB_URL).ensure_schema()

    tid = uuid.uuid4().hex
    with psycopg.connect(DB_URL, autocommit=True) as c:
        # A legacy-shaped tenant: tenants.api_key populated, but NO tenant_api_keys row.
        c.execute(
            "INSERT INTO tenants (id, name, plan, status, api_key) "
            "VALUES (%s, %s, 'starter', 'active', %s)",
            (tid, "legacy-tenant", f"gfm_{uuid.uuid4().hex}"))
        pre = c.execute(
            "SELECT count(*) FROM tenant_api_keys WHERE tenant_id = %s", (tid,)).fetchone()[0]
        assert pre == 0  # sanity: no key row yet

    # 2. ensure_schema must NOT mint from tenants.api_key.
    TenantManager(DB_URL).ensure_schema()

    with psycopg.connect(DB_URL, autocommit=True) as c:
        n = c.execute(
            "SELECT count(*) FROM tenant_api_keys WHERE tenant_id = %s", (tid,)).fetchone()[0]
    assert n == 0, (
        f"ensure_schema minted {n} tenant_api_keys row(s) from tenants.api_key — the backfill "
        f"must stay removed; tenants.api_key is not a minting source (014)"
    )
