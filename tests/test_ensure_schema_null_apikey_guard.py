"""Regression: TenantManager.ensure_schema must tolerate a NULL-api_key tenant.

After migration 014 (tenants.api_key nullable + writers stop populating it), tenants can
have api_key = NULL. The ensure_schema backfill
    INSERT INTO tenant_api_keys (...) SELECT ... api_key ... FROM tenants
must skip those rows (WHERE api_key IS NOT NULL); otherwise it raises NotNullViolation on
tenant_api_keys.api_key and the pre-deploy fails. This guard is what makes main
rollback-compatible with post-014 data.

Without the guard this test raises NotNullViolation; with it, it passes.
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


def test_ensure_schema_tolerates_null_api_key_tenant(fresh_tables):
    # 1. Create the tables.
    TenantManager(DB_URL).ensure_schema()

    null_id = uuid.uuid4().hex
    good_id = uuid.uuid4().hex
    good_key = f"gfm_{uuid.uuid4().hex}"

    with psycopg.connect(DB_URL, autocommit=True) as c:
        # 2. Simulate migration 014: api_key becomes nullable.
        c.execute("ALTER TABLE tenants ALTER COLUMN api_key DROP NOT NULL")
        # A post-014 tenant: NULL api_key, key already lives in tenant_api_keys.
        c.execute(
            "INSERT INTO tenants (id, name, plan, status, api_key) "
            "VALUES (%s, %s, 'starter', 'active', NULL)", (null_id, "null-tenant"))
        c.execute(
            "INSERT INTO tenant_api_keys (key_id, tenant_id, api_key, name, role) "
            "VALUES (gen_random_uuid()::text, %s, %s, 'Default Admin Key', 'admin')",
            (null_id, f"gfm_{uuid.uuid4().hex}"))
        # A legacy tenant: real api_key, no key row yet — the backfill SHOULD create one.
        c.execute(
            "INSERT INTO tenants (id, name, plan, status, api_key) "
            "VALUES (%s, %s, 'starter', 'active', %s)", (good_id, "good-tenant", good_key))

    # 3. ensure_schema must NOT raise on the NULL-api_key tenant.
    TenantManager(DB_URL).ensure_schema()

    with psycopg.connect(DB_URL, autocommit=True) as c:
        # NULL tenant was skipped by the backfill — still exactly its original one key row.
        n_null = c.execute(
            "SELECT count(*) FROM tenant_api_keys WHERE tenant_id = %s", (null_id,)).fetchone()[0]
        assert n_null == 1, f"expected the NULL-api_key tenant untouched, got {n_null} key rows"
        # Legacy tenant WAS backfilled — the guard must not over-skip valid rows.
        n_good = c.execute(
            "SELECT count(*) FROM tenant_api_keys WHERE tenant_id = %s AND api_key = %s",
            (good_id, good_key)).fetchone()[0]
        assert n_good == 1, "backfill should still create a key row for a non-NULL-api_key tenant"
