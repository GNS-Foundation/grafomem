"""B8: tenants.api_key — nulled by 015, never written or displayed since 014 — is dropped by the HELD
migration 019, and the server no longer selects it.

MUST-FAIL on main 9d7a49a: get_tenant/list_tenants still `SELECT … api_key … FROM tenants`
(tenant_manager.py:280, :321) so they fail on a database without the column; held/019 does not exist
(the runner refuses it as "not a held migration"); and _SCHEMA_SQL still creates the column.

Four tests: (1) 019 is held, never in the ordinary pass; (2) a fresh database through 015 and held 019
with the runner's refusals (no --confirm-irreversible; a non-NULL value present); (3) tenant reads,
portal signup/login//v1/portal/me and /v1/cloud/tenants work without the column; (4) SSO create and
create_tenant work without the column. Nothing here applies 019 outside a throwaway test database.
"""
import os
import pathlib
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from aml.cloud.tenant_manager import TenantManager

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS = _ROOT / "src/aml/cloud/migrations"
HELD_019 = "019_drop_tenants_api_key.sql"


def _has_column(url: str, table: str, column: str) -> bool:
    with psycopg.connect(url) as c:
        return c.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = %s AND column_name = %s", (table, column)).fetchone() is not None


# ---------------------------------------------------------------- 1. held, never ordinary

def test_019_is_held_and_not_in_the_ordinary_pass():
    from aml.cloud.migrations_runner import _held_dir, _sql_files
    names = [f.name for f in _sql_files(_MIGRATIONS)]
    assert not any("019" in n for n in names), "019 must never be listed for the ordinary pass"
    assert (_held_dir(_MIGRATIONS) / HELD_019).is_file(), f"held/{HELD_019} must exist"


# ---------------------------------------------------------------- 2. fresh database through 015 and 019

def _admin_url() -> str:
    return DB_URL.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture
def fresh_db():
    name = f"b8_fresh_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_admin_url(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    url = DB_URL.rsplit("/", 1)[0] + f"/{name}"
    yield url
    with psycopg.connect(_admin_url(), autocommit=True) as c:
        c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_fresh_database_through_015_and_held_019(fresh_db, monkeypatch):
    from aml.cloud.migrations_runner import HeldMigrationRefused, apply_held_migration, apply_migrations
    from aml.server.app import create_app
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER", "b8-pepper-" + "c" * 40)
    url = fresh_db
    create_app(db_url=url, ensure_schema_only=True)          # the A1 release step: every ensure_schema
    assert not _has_column(url, "tenants", "api_key"), "a fresh ensure_schema must not create tenants.api_key"
    # A migrated production-shaped database still HAS the column (014 nullable, 015 nulled): recreate it.
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("ALTER TABLE tenants ADD COLUMN api_key TEXT UNIQUE")
    res = apply_migrations(url, migrations_dir=_MIGRATIONS)   # 014 and 015 must run (or skip) cleanly
    assert "015_null_tenants_api_key.sql" in res["applied"] + res["skipped"]
    assert not any("019" in v for v in res["applied"] + res["skipped"]), "the ordinary pass must not touch 019"
    m = TenantManager(url)
    t = m.create_tenant(name="b8-fresh")
    # refusals: no confirmation; a non-NULL value present
    with pytest.raises(HeldMigrationRefused, match="confirm-irreversible"):
        apply_held_migration(url, HELD_019, migrations_dir=_MIGRATIONS)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE tenants SET api_key = %s WHERE id = %s", ("gfm_residual_" + uuid.uuid4().hex, t.id))
    with pytest.raises(HeldMigrationRefused, match="non-NULL"):
        apply_held_migration(url, HELD_019, confirm_irreversible=True, migrations_dir=_MIGRATIONS)
    assert _has_column(url, "tenants", "api_key"), "a refused held migration writes nothing"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE tenants SET api_key = NULL WHERE id = %s", (t.id,))
    res = apply_held_migration(url, HELD_019, confirm_irreversible=True, migrations_dir=_MIGRATIONS)
    assert res["applied"] == [HELD_019]
    assert not _has_column(url, "tenants", "api_key")
    with psycopg.connect(url, row_factory=dict_row) as c:
        led = c.execute("SELECT applied_via FROM schema_migrations WHERE version = %s", (HELD_019,)).fetchone()
    assert led and led["applied_via"] == "held"
    # the server still reads tenants without the column
    assert m.get_tenant(t.id) is not None and m.get_tenant(t.id).api_key == ""
    assert any(x.id == t.id for x in m.list_tenants())
    # a second run is a recorded skip, not an error
    assert apply_held_migration(url, HELD_019, confirm_irreversible=True, migrations_dir=_MIGRATIONS) == {"applied": [], "skipped": [HELD_019]}


# ---------------------------------------------------------------- 3./4. the shared test DB without the column

@pytest.fixture
def column_dropped():
    """Shape of every database after 019: no tenants.api_key. The test DB keeps this shape afterwards
    (nothing reads the column any more); tests that exercise 015 recreate it themselves."""
    TenantManager(DB_URL).ensure_schema()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS api_key")
    yield


@pytest.fixture(scope="module")
def app_instance():
    os.environ["GRAFOMEM_DB_URL"] = DB_URL
    os.environ["GRAFOMEM_AUTH_MODE"] = "cloud"
    os.environ.setdefault("GRAFOMEM_SIGNING_KEY", "b" * 64)
    os.environ.setdefault("GRAFOMEM_PORTAL_SECRET", "b8-test-secret-" + "x" * 24)
    from aml.server.app import create_app
    return create_app(db_url=DB_URL)


def test_tenant_reads_and_portal_work_without_column(column_dropped, app_instance, monkeypatch):
    m = TenantManager(DB_URL)
    t = m.create_tenant(name="b8-no-col")
    info = m.get_tenant(t.id)                                   # red on main: UndefinedColumn api_key
    assert info is not None and info.api_key == ""
    assert any(x.id == t.id for x in m.list_tenants())
    # platform read of /v1/cloud/tenants: a key of a PLATFORM_TENANT_IDS tenant
    monkeypatch.setenv("PLATFORM_TENANT_IDS", t.id)
    k = m.create_api_key(t.id, name="b8-platform", role="admin", scopes=["*"])["api_key"]
    with TestClient(app_instance) as c:
        assert c.get("/health").status_code == 200
        r = c.get("/v1/cloud/tenants", headers={"X-API-Key": k})
        assert r.status_code == 200, r.text
        assert all("api_key" not in row for row in r.json())
        email = f"b8-{uuid.uuid4().hex[:8]}@example.com"
        r = c.post("/v1/portal/signup", json={"name": "B8", "email": email, "password": "password-b8-1"})
        assert r.status_code == 201, r.text
        r = c.post("/v1/portal/login", json={"email": email, "password": "password-b8-1"})
        assert r.status_code == 200, r.text
        tok = r.json()["token"]
        r = c.get("/v1/portal/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        assert "api_key" not in r.json()


def test_sso_create_and_create_tenant_work_without_column(column_dropped):
    from aml.cloud.sso_provider import SSOProvider
    p = SSOProvider(DB_URL)
    p.ensure_schema()
    email = f"b8-sso-{uuid.uuid4().hex[:8]}@example.test"
    tid, key = p._find_or_create_tenant(email, "B8 SSO", "google", f"sub-{uuid.uuid4().hex[:8]}")
    assert key and key.startswith("gfm_")
    m = TenantManager(DB_URL)
    assert m.get_tenant(tid) is not None                        # red on main: UndefinedColumn api_key
    t = m.create_tenant(name="b8-ct")
    assert m.get_tenant(t.id).api_key == ""
