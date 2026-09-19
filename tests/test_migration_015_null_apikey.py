"""Migration 015 nulls residual tenants.api_key values, leaving tenant_api_keys untouched.

014 retired tenants.api_key (nullable, unread); 015 nulls any residual plaintext before 016
drops the column. The authoritative key in tenant_api_keys must be untouched.
"""
import os
import pathlib
import uuid

import psycopg
import pytest

from aml.cloud.tenant_manager import TenantManager

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
MIG_015 = pathlib.Path(__file__).resolve().parents[1] / \
    "src/aml/cloud/migrations/015_null_tenants_api_key.sql"


@pytest.fixture
def fresh_tables():
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS tenant_api_keys CASCADE")
        c.execute("DROP TABLE IF EXISTS tenants CASCADE")
    yield


def test_015_nulls_residual_tenants_api_key(fresh_tables):
    TenantManager(DB_URL).ensure_schema()  # tenants.api_key is nullable (post-014 _SCHEMA_SQL)

    tid = uuid.uuid4().hex
    residual = f"gfm_{uuid.uuid4().hex}"       # a stale plaintext value in tenants.api_key
    live_key = f"gfm_{uuid.uuid4().hex}"       # the authoritative key in tenant_api_keys
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("INSERT INTO tenants (id, name, plan, status, api_key) "
                  "VALUES (%s, %s, 'starter', 'active', %s)", (tid, "residual", residual))
        c.execute("INSERT INTO tenant_api_keys (key_id, tenant_id, api_key, name, role) "
                  "VALUES (gen_random_uuid()::text, %s, %s, 'Default Admin Key', 'admin')",
                  (tid, live_key))
        pre = c.execute("SELECT count(*) FROM tenants WHERE api_key IS NOT NULL").fetchone()[0]
        assert pre >= 1  # non-vacuity: there IS a residual value to null

        c.execute(MIG_015.read_text())

        col = c.execute("SELECT api_key FROM tenants WHERE id = %s", (tid,)).fetchone()[0]
        assert col is None, f"015 must null tenants.api_key, got {col!r}"
        assert c.execute("SELECT count(*) FROM tenants WHERE api_key IS NOT NULL").fetchone()[0] == 0
        # tenant_api_keys is the source of truth and must be UNTOUCHED.
        row = c.execute("SELECT api_key FROM tenant_api_keys WHERE tenant_id = %s", (tid,)).fetchone()
        assert row is not None and row[0] == live_key, "015 must not touch tenant_api_keys"

        # Idempotent: a second run nulls nothing new and does not error.
        c.execute(MIG_015.read_text())
        assert c.execute("SELECT count(*) FROM tenants WHERE api_key IS NOT NULL").fetchone()[0] == 0
