"""Hash-at-rest PR 2 — api_key_hash column + runner-side backfill. PR 5 update: mints no longer store
the plaintext, so the backfill tests seed legacy-shaped rows (plaintext present, hash NULL) themselves
and SKIP once the plaintext column is gone (held migration 018b), when the backfill is a clean no-op.

Must-fails: backfill refuses with the pepper absent/empty; the pepper never appears in any SQL the
backfill sends; a backfilled row's hash verifies against its plaintext with the runner-side HMAC.
Positive control: auth resolves a backfilled key by its hash.
"""
import importlib.util
import os
import pathlib
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from aml.cloud.tenant_manager import TenantManager
from aml.server.api_key_hash import PEPPER_ENV, PepperMissing, compute_api_key_hash, get_pepper
from aml.server.auth import TenantAuthMiddleware

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
PEPPER = "test-pepper-" + "d" * 40

# Load the backfill script by path (scripts/ is not a package).
_spec = importlib.util.spec_from_file_location(
    "backfill_api_key_hash", _ROOT / "scripts" / "backfill_api_key_hash.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


def _apply_migration_017():
    sql = (_ROOT / "src/aml/cloud/migrations/017_api_key_hash.sql").read_text()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute(sql)


def _plaintext_column_present() -> bool:
    with psycopg.connect(DB_URL, autocommit=True) as c:
        return bool(c.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name='tenant_api_keys' AND column_name='api_key'").fetchone())


def _seed_legacy_row(tenant_id: str) -> str:
    """A pre-hash-at-rest row: plaintext present, api_key_hash NULL — what the backfill exists for."""
    key = f"gfm_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("INSERT INTO tenant_api_keys (key_id,tenant_id,api_key,name,role,scopes) "
                  "VALUES (%s,%s,%s,'legacy','agent',%s)", (uuid.uuid4().hex, tenant_id, key, ["cgr:read"]))
    return key


@pytest.fixture(scope="module")
def setup():
    os.environ[PEPPER_ENV] = PEPPER
    tm = TenantManager(DB_URL)
    tm.ensure_schema()
    _apply_migration_017()
    info = tm.create_tenant(name=f"hashbf-{uuid.uuid4().hex[:8]}")
    tm.create_api_key(info.id, name="agent", role="agent")
    legacy = _seed_legacy_row(info.id) if _plaintext_column_present() else None
    return {"tenant_id": info.id, "api_key": info.api_key, "legacy_key": legacy}


@pytest.fixture
def needs_plaintext_column(setup):
    if not _plaintext_column_present():
        pytest.skip("plaintext column dropped (018b): the backfill is a no-op, nothing to backfill")


class _Recorder:
    """Wraps a psycopg connection, recording every SQL string passed to execute()."""
    def __init__(self, conn):
        self._conn = conn
        self.sql_log: list[str] = []

    def execute(self, sql, params=None):
        self.sql_log.append(sql)
        return self._conn.execute(sql, params) if params is not None else self._conn.execute(sql)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_backfill_refuses_when_pepper_absent(monkeypatch):
    """MUST-FAIL: no pepper → get_pepper raises and main() refuses (exit 3), no DB touched."""
    monkeypatch.delenv(PEPPER_ENV, raising=False)
    with pytest.raises(PepperMissing):
        get_pepper()
    assert bf.main([]) == 3


def test_backfill_refuses_when_pepper_empty(monkeypatch):
    """MUST-FAIL: empty pepper is treated as absent (fail closed)."""
    monkeypatch.setenv(PEPPER_ENV, "")
    with pytest.raises(PepperMissing):
        get_pepper()
    assert bf.main([]) == 3


def test_backfill_skip_gate_is_dark_when_pepper_absent(monkeypatch):
    """Pre-deploy gate: --skip-if-no-pepper stays DARK (exit 0, no writes) if the pepper is absent —
    so a pepper-less environment does not block the deploy. Direct invocation still fail-closes."""
    monkeypatch.delenv(PEPPER_ENV, raising=False)
    assert bf.main(["--skip-if-no-pepper"]) == 0


def test_pepper_never_appears_in_sql(setup, needs_plaintext_column):
    """MUST-FAIL guard: the pepper is HMAC input in-process only — it must not appear in any SQL the
    backfill sends (the pg_stat_statements / query-log leak the design forbids)."""
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as raw:
        rec = _Recorder(raw)
        bf.backfill(rec, PEPPER)
        assert rec.sql_log, "backfill issued no SQL"
        for sql in rec.sql_log:
            assert PEPPER not in sql, f"pepper leaked into SQL: {sql!r}"
            assert "hmac" not in sql.lower(), f"SQL computed a hash server-side: {sql!r}"


def test_backfilled_hash_verifies_against_plaintext(setup, needs_plaintext_column):
    """A backfilled legacy row's stored api_key_hash equals HMAC_SHA256(pepper, its plaintext api_key)."""
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        bf.backfill(c, PEPPER)
        rows = c.execute(
            "SELECT api_key, api_key_hash FROM tenant_api_keys WHERE tenant_id = %s AND api_key IS NOT NULL",
            (setup["tenant_id"],)).fetchall()
    assert rows, "the legacy-shaped row must be present"
    for r in rows:
        assert r["api_key_hash"] is not None, "row not backfilled"
        expected = compute_api_key_hash(r["api_key"], PEPPER)
        assert bytes(r["api_key_hash"]) == expected, "stored hash != runner-side HMAC of plaintext"


def test_skip_logs_a_visible_warning(monkeypatch, caplog):
    """CONFIRMATION 1: a pepper-less pre-deploy is NOT silent — the skip logs an explicit WARNING."""
    import logging
    from aml.cloud import api_key_hash_backfill as mod
    monkeypatch.delenv(PEPPER_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger="grafomem.migrations.apikeyhash"):
        res = mod.run("postgresql://unused", skip_if_no_pepper=True)
    assert res.get("skipped") is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("SKIPPED" in r.getMessage() and PEPPER_ENV in r.getMessage() for r in warnings), \
        "skip must emit a visible WARNING naming the pepper env var"


def test_backfill_is_idempotent(setup, needs_plaintext_column):
    """CONFIRMATION 2: backfill selects only WHERE api_key_hash IS NULL, so a SECOND run updates 0."""
    kid = uuid.uuid4().hex
    key = f"gfm_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        c.execute("INSERT INTO tenant_api_keys (key_id,tenant_id,api_key,name,role,scopes) "
                  "VALUES (%s,%s,%s,'idem','agent',%s)", (kid, setup["tenant_id"], key, ["cgr:read"]))
        first = bf.backfill(c, PEPPER)
        assert first["updated"] >= 1, "first run must populate the new NULL row"
        second = bf.backfill(c, PEPPER)
        assert second["updated"] == 0, "second run updates 0 (WHERE api_key_hash IS NULL selects nothing)"


def test_api_key_hash_unique_index_enforced(setup):
    """CONFIRMATION 3 (evidence): the UNIQUE index on api_key_hash is enforced — a duplicate hash is
    rejected by the DB. (In practice tenant_api_keys.api_key is itself UNIQUE, so distinct plaintexts
    give distinct HMACs and this never fires; if a dup ever existed the backfill's UPDATE would raise,
    roll back, and the pre-deploy would exit non-zero → deploy blocked, previous build keeps serving.)"""
    import psycopg.errors
    h = compute_api_key_hash("gfm_" + uuid.uuid4().hex, PEPPER)
    with psycopg.connect(DB_URL, autocommit=True) as c:
        k1, k2 = uuid.uuid4().hex, uuid.uuid4().hex
        c.execute("INSERT INTO tenant_api_keys (key_id,tenant_id,name,role,scopes,api_key_hash) "
                  "VALUES (%s,%s,'h1','agent',%s,%s)",
                  (k1, setup["tenant_id"], ["cgr:read"], h))
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("INSERT INTO tenant_api_keys (key_id,tenant_id,name,role,scopes,api_key_hash) "
                      "VALUES (%s,%s,'h2','agent',%s,%s)",
                      (k2, setup["tenant_id"], ["cgr:read"], h))


def test_auth_resolves_backfilled_legacy_key_by_hash(setup, needs_plaintext_column, monkeypatch):
    """POSITIVE CONTROL (PR 5): a legacy-shaped row (plaintext, NULL hash) does NOT resolve until the
    backfill hashes it; afterwards it resolves — by its hash, the only path."""
    monkeypatch.setenv(PEPPER_ENV, PEPPER)
    legacy = setup["legacy_key"]
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("UPDATE tenant_api_keys SET api_key_hash = NULL WHERE tenant_id = %s AND api_key = %s",
                  (setup["tenant_id"], legacy))
    mw = TenantAuthMiddleware(app=None, auth_mode="cloud", db_url=DB_URL)
    assert mw._resolve_api_key(legacy) is None, "no plaintext fallback: a NULL-hash row must not resolve"
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        bf.backfill(c, PEPPER)
    resolved = mw._resolve_api_key(legacy)
    assert resolved is not None and resolved[0] == setup["tenant_id"]
