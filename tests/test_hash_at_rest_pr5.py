"""Hash-at-rest PR 5 — the plaintext API key is no longer stored or read; keys resolve by hash ONLY.

MUST-FAIL FIRST (each fails on main before this PR, passes after):
  1. a key resolves ONLY via its hash — and the row's plaintext column is NULL after mint;
  2. a NULL-hash key gets 403 (no plaintext fallback resolves it);
  3. a key minted by scripts/rotate_all_tenant_keys.py authenticates (the script now writes the hash);
  4. mint works with api_key NULL on every mint path (tenant birth key, create_api_key, portal signup,
     SSO provisioning) — the plaintext never reaches the database;
  5. a FRESH database migrated through 018a (ordinary) and 018b (held, explicit) passes: the plaintext
     column is gone, mint + resolve still work, the pre-deploy backfill is a clean no-op.
Plus the held-migration guard rails: 018b is never part of the ordinary pass, refuses without
--confirm-irreversible, and refuses while any api_key_hash IS NULL.
"""
import importlib.util
import os
import pathlib
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from aml.cloud.tenant_manager import TenantManager
from aml.server.auth import TenantAuthMiddleware
from aml.server.api_key_hash import PEPPER_ENV, PepperMissing, compute_api_key_hash
from aml.server.scopes import TENANT_ADMIN_SCOPES

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS = _ROOT / "src/aml/cloud/migrations"
PEPPER = "pr5-pepper-" + "a" * 40

# The rotate script, loaded by path (scripts/ is not a package).
_SPEC = importlib.util.spec_from_file_location(
    "rotate_all_tenant_keys", _ROOT / "scripts" / "rotate_all_tenant_keys.py")
rot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rot)


def _apply(sql_name: str):
    path = _MIGRATIONS / sql_name
    if not path.exists():  # on main before this PR 018a does not exist: the assertions below fail for the real reasons
        return
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute(path.read_text())


@pytest.fixture(scope="module")
def tm():
    os.environ[PEPPER_ENV] = PEPPER
    m = TenantManager(DB_URL)
    m.ensure_schema()
    _apply("017_api_key_hash.sql")
    _apply("018a_api_key_plaintext_nullable.sql")
    return m


@pytest.fixture
def pepper(monkeypatch):
    monkeypatch.setenv(PEPPER_ENV, PEPPER)
    monkeypatch.delenv("GRAFOMEM_API_KEY_PEPPER_RETIRING", raising=False)


@pytest.fixture
def tenant(tm, pepper):
    return tm.create_tenant(name=f"pr5-{uuid.uuid4().hex[:8]}")


def _row(key_id: str, url: str = DB_URL) -> dict | None:
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as c:
        cols = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='tenant_api_keys'").fetchall()}
        sel = "key_id, api_key_hash" + (", api_key" if "api_key" in cols else ", NULL::text AS api_key")
        return c.execute(f"SELECT {sel} FROM tenant_api_keys WHERE key_id=%s", (key_id,)).fetchone()


def _mw(url: str = DB_URL) -> TenantAuthMiddleware:
    return TenantAuthMiddleware(app=None, auth_mode="cloud", db_url=url)


def _lookup(mw, api_key, url: str = DB_URL):
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as c:
        return mw._lookup_key_row(c, api_key)


# ── 1. resolves ONLY via hash; plaintext column NULL after mint ──────────────────────────

def test_key_resolves_only_via_hash_and_plaintext_is_not_stored(tenant, tm, pepper):
    info = tm.create_api_key(tenant.id, name="k", role="agent", scopes=["cgr:read"])
    row = _row(info["key_id"])
    assert row["api_key"] is None, "the plaintext must not be stored (main stores it)"
    assert bytes(row["api_key_hash"]) == compute_api_key_hash(info["api_key"], PEPPER)
    mw = _mw()
    r, via = _lookup(mw, info["api_key"])
    assert r is not None and via == "hash"
    assert mw._resolve_api_key(info["api_key"])[0] == tenant.id
    assert mw.plaintext_path_resolutions == 0


# ── 2. a NULL-hash key gets 403 ─────────────────────────────────────────────────────────

def test_null_hash_key_gets_403(tenant, tm, pepper):
    info = tm.create_api_key(tenant.id, name="dead", role="agent", scopes=["cgr:read"])
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("UPDATE tenant_api_keys SET api_key_hash = NULL WHERE key_id = %s", (info["key_id"],))
    mw = _mw()
    assert _lookup(mw, info["api_key"]) == (None, None), "no plaintext fallback may resolve a NULL-hash key"
    assert mw._resolve_api_key(info["api_key"]) is None  # → the middleware answers 403 (invalid key)
    # the HTTP mapping, end to end
    from aml.server.app import create_app
    from fastapi.testclient import TestClient
    app = create_app(db_url=DB_URL, spec_only=True)
    with TestClient(app) as client:
        assert client.get("/v1/stores", headers={"X-API-Key": info["api_key"]}).status_code == 403
        # positive control: a sibling key with its hash intact is accepted
        ok = tm.create_api_key(tenant.id, name="alive", role="admin", scopes=TENANT_ADMIN_SCOPES)
        assert client.get("/v1/stores", headers={"X-API-Key": ok["api_key"]}).status_code == 200


# ── 3. a key minted by rotate_all_tenant_keys.py authenticates ───────────────────────────

def test_rotate_script_minted_key_authenticates(tenant, tm, pepper, tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFOMEM_ROTATE_DB_URL", DB_URL)
    monkeypatch.setenv("PLATFORM_TENANT_IDS", tenant.id)
    out = tmp_path / "k.jsonl"
    with psycopg.connect(DB_URL, autocommit=True) as c:  # tuple rows, as the script's own main() connects
        n = rot._mint_only_live(c, [tenant.id], [], {"name": "ci-key", "role": "agent", "scopes": ["cgr:read"]}, str(out))
    assert n == 1
    import json
    minted = json.loads(out.read_text().splitlines()[0])
    row = _row(minted["new_key_id"])
    assert row["api_key"] is None and row["api_key_hash"] is not None, "script must write the hash, not the plaintext"
    r, via = _lookup(_mw(), minted["api_key"])
    assert r is not None and via == "hash" and r["key_id"] == minted["new_key_id"]


def test_rotate_script_validates_scopes(tenant, pepper, tmp_path):
    with psycopg.connect(DB_URL, autocommit=True) as c:
        with pytest.raises(ValueError, match="Invalid scopes"):
            rot._mint_only_live(c, [tenant.id], [], {"name": "bad", "role": "agent", "scopes": ["not:a:scope"]},
                                str(tmp_path / "bad.jsonl"))


def test_rotate_script_refuses_without_pepper(tenant, tmp_path, monkeypatch):
    monkeypatch.delenv(PEPPER_ENV, raising=False)
    with psycopg.connect(DB_URL, autocommit=True) as c:
        with pytest.raises(SystemExit, match="refusing to mint"):
            rot._mint_only_live(c, [tenant.id], [], {"name": "x", "role": "agent", "scopes": ["cgr:read"]},
                                str(tmp_path / "x.jsonl"))


# ── 4. mint works with api_key NULL on every path ────────────────────────────────────────

def test_birth_key_and_create_api_key_store_no_plaintext(tm, pepper):
    t = tm.create_tenant(name=f"birth-{uuid.uuid4().hex[:6]}")
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        rows = c.execute("SELECT key_id, api_key, api_key_hash FROM tenant_api_keys WHERE tenant_id=%s", (t.id,)).fetchall()
    assert len(rows) == 1 and rows[0]["api_key"] is None and rows[0]["api_key_hash"] is not None
    assert _lookup(_mw(), t.api_key)[1] == "hash"


def test_portal_signup_stores_no_plaintext(pepper):
    pytest.importorskip("bcrypt")
    from aml.cloud.portal_auth import PortalAuth
    pa = PortalAuth(DB_URL, secret_key="test-secret")
    pa.ensure_schema()
    info, _token = pa.signup(name="PR5", email=f"pr5-{uuid.uuid4().hex[:8]}@example.com", password="password123")
    assert info["api_key"].startswith("gfm_"), "signup still returns the birth key show-once"
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        rows = c.execute("SELECT api_key, api_key_hash FROM tenant_api_keys WHERE tenant_id=%s", (info["tenant_id"],)).fetchall()
    assert rows and all(r["api_key"] is None and r["api_key_hash"] is not None for r in rows)
    assert _lookup(_mw(), info["api_key"])[1] == "hash"


def test_sso_provisioning_stores_no_plaintext(pepper):
    pytest.importorskip("bcrypt")
    from aml.cloud.portal_auth import PortalAuth
    pa = PortalAuth(DB_URL, secret_key="test-secret")
    pa.ensure_schema()
    first = pa.ensure_tenant(supabase_uid=f"sub-{uuid.uuid4().hex[:10]}", email=f"sso-{uuid.uuid4().hex[:8]}@example.com", name="SSO Org")
    assert first["api_key"], "first provision is show-once"
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        rows = c.execute("SELECT api_key, api_key_hash FROM tenant_api_keys WHERE tenant_id=%s", (first["tenant_id"],)).fetchall()
    assert rows and all(r["api_key"] is None and r["api_key_hash"] is not None for r in rows)


def test_mint_refuses_without_pepper(tenant, tm, monkeypatch):
    monkeypatch.delenv(PEPPER_ENV, raising=False)
    with pytest.raises(PepperMissing):
        tm.create_api_key(tenant.id, name="nopepper", role="agent", scopes=["cgr:read"])


def test_revoke_by_id_returns_key_id_and_revoke_by_value_matches_hash(tenant, tm, pepper):
    a = tm.create_api_key(tenant.id, name="a", role="agent", scopes=["cgr:read"])
    b = tm.create_api_key(tenant.id, name="b", role="agent", scopes=["cgr:read"])
    assert tm.revoke_key_by_id(a["key_id"], tenant.id) == a["key_id"]  # #188 site: no RETURNING api_key
    assert tm.revoke_key(b["api_key"]) == b["api_key"]                   # matched by hash
    assert _row(a["key_id"]) is None and _row(b["key_id"]) is None


# ── held migration guard rails ───────────────────────────────────────────────────────────

def test_018b_is_not_in_the_ordinary_pass():
    from aml.cloud.migrations_runner import _sql_files, _held_dir
    names = [f.name for f in _sql_files(_MIGRATIONS)]
    assert "018a_api_key_plaintext_nullable.sql" in names
    assert not any("018b" in n for n in names), "018b must never be listed for the ordinary pass"
    assert (_held_dir(_MIGRATIONS) / "018b_drop_api_key_plaintext.sql").is_file()


# ── 5. fresh database through 018a and 018b ──────────────────────────────────────────────

def _admin_url() -> str:
    return DB_URL.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture
def fresh_db():
    name = f"pr5_fresh_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_admin_url(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    url = DB_URL.rsplit("/", 1)[0] + f"/{name}"
    yield url
    with psycopg.connect(_admin_url(), autocommit=True) as c:
        c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_fresh_database_through_018a_and_018b(fresh_db, pepper):
    from aml.cloud.migrations_runner import (apply_migrations, apply_held_migration, HeldMigrationRefused,
                                             HELD_SUBDIR)
    from aml.cloud.api_key_hash_backfill import run as backfill_run
    url = fresh_db
    # the A1 release step: every service's ensure_schema (as the runner's --ensure-schema does), then migrations
    from aml.server.app import create_app
    create_app(db_url=url, ensure_schema_only=True)
    m = TenantManager(url)
    # fresh ensure_schema: the plaintext column exists, nullable, and the plaintext index is not created
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as c:
        col = c.execute("SELECT is_nullable FROM information_schema.columns WHERE table_name='tenant_api_keys' AND column_name='api_key'").fetchone()
        assert col and col["is_nullable"] == "YES"
        assert not c.execute("SELECT 1 FROM pg_indexes WHERE indexname='idx_tenant_api_keys_key'").fetchone()
    res = apply_migrations(url, migrations_dir=_MIGRATIONS)
    assert "018a_api_key_plaintext_nullable.sql" in res["applied"] + res["skipped"]
    assert not any("018b" in v for v in res["applied"] + res["skipped"]), "the ordinary pass must not touch 018b"
    t = m.create_tenant(name="fresh")
    k = m.create_api_key(t.id, name="k", role="agent", scopes=["cgr:read"])
    # guard rails: refused without confirmation; refused while a NULL-hash row exists
    with pytest.raises(HeldMigrationRefused, match="confirm-irreversible"):
        apply_held_migration(url, "018b_drop_api_key_plaintext.sql", migrations_dir=_MIGRATIONS)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE tenant_api_keys SET api_key_hash = NULL WHERE key_id = %s", (k["key_id"],))
    with pytest.raises(HeldMigrationRefused, match="api_key_hash IS NULL"):
        apply_held_migration(url, "018b_drop_api_key_plaintext.sql", confirm_irreversible=True, migrations_dir=_MIGRATIONS)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE tenant_api_keys SET api_key_hash = %s WHERE key_id = %s",
                  (compute_api_key_hash(k["api_key"], PEPPER), k["key_id"]))
    with pytest.raises(HeldMigrationRefused, match="not a held migration"):
        apply_held_migration(url, "018a_api_key_plaintext_nullable.sql", confirm_irreversible=True, migrations_dir=_MIGRATIONS)
    # the explicit, confirmed apply
    res = apply_held_migration(url, "018b_drop_api_key_plaintext.sql", confirm_irreversible=True, migrations_dir=_MIGRATIONS)
    assert res["applied"] == ["018b_drop_api_key_plaintext.sql"]
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as c:
        assert not c.execute("SELECT 1 FROM information_schema.columns WHERE table_name='tenant_api_keys' AND column_name='api_key'").fetchone()
        assert c.execute("SELECT applied_via FROM schema_migrations WHERE version=%s", ("018b_drop_api_key_plaintext.sql",)).fetchone()["applied_via"] == "held"
    # second apply is a recorded no-op; mint + resolve keep working; the pre-deploy backfill is a no-op
    assert apply_held_migration(url, "018b_drop_api_key_plaintext.sql", confirm_irreversible=True, migrations_dir=_MIGRATIONS)["skipped"] == ["018b_drop_api_key_plaintext.sql"]
    k2 = m.create_api_key(t.id, name="after-drop", role="agent", scopes=["cgr:read"])
    assert _lookup(_mw(url), k2["api_key"], url)[1] == "hash"
    assert _lookup(_mw(url), k["api_key"], url)[1] == "hash"
    assert backfill_run(url).get("column_dropped") is True
    # ensure_schema is still idempotent on the post-018b schema (no plaintext index recreated)
    m.ensure_schema()
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as c:
        assert not c.execute("SELECT 1 FROM information_schema.columns WHERE table_name='tenant_api_keys' AND column_name='api_key'").fetchone()
    assert HELD_SUBDIR == "held"
