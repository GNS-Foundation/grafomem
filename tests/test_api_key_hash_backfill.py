"""Hash-at-rest PR 2 — api_key_hash column + runner-side backfill (DARK, auth still plaintext).

Must-fails (each fails on the pre-PR behaviour): backfill refuses with the pepper absent/empty; the
pepper never appears in any SQL the backfill sends; a backfilled row's hash verifies against its
plaintext with the runner-side HMAC. Positive control: auth still resolves every key by plaintext
after the migration + backfill (the change is dark).
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


@pytest.fixture(scope="module")
def setup():
    tm = TenantManager(DB_URL)
    tm.ensure_schema()
    _apply_migration_017()
    info = tm.create_tenant(name=f"hashbf-{uuid.uuid4().hex[:8]}")
    # A couple of extra keys so the backfill has real rows to populate.
    tm.create_api_key(info.id, name="agent", role="agent")
    return {"tenant_id": info.id, "api_key": info.api_key}


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


def test_pepper_never_appears_in_sql(setup):
    """MUST-FAIL guard: the pepper is HMAC input in-process only — it must not appear in any SQL the
    backfill sends (the pg_stat_statements / query-log leak the design forbids)."""
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as raw:
        rec = _Recorder(raw)
        bf.backfill(rec, PEPPER)
        assert rec.sql_log, "backfill issued no SQL"
        for sql in rec.sql_log:
            assert PEPPER not in sql, f"pepper leaked into SQL: {sql!r}"
            assert "hmac" not in sql.lower(), f"SQL computed a hash server-side: {sql!r}"


def test_backfilled_hash_verifies_against_plaintext(setup):
    """A backfilled row's stored api_key_hash equals HMAC_SHA256(pepper, its plaintext api_key)."""
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        bf.backfill(c, PEPPER)
        rows = c.execute(
            "SELECT api_key, api_key_hash FROM tenant_api_keys WHERE tenant_id = %s",
            (setup["tenant_id"],)).fetchall()
    assert rows
    for r in rows:
        assert r["api_key_hash"] is not None, "row not backfilled"
        expected = compute_api_key_hash(r["api_key"], PEPPER)
        assert bytes(r["api_key_hash"]) == expected, "stored hash != runner-side HMAC of plaintext"


def test_auth_still_resolves_by_plaintext_after_backfill(setup):
    """POSITIVE CONTROL: the change is DARK — auth still resolves the key by its plaintext after the
    column + backfill (auth does not read api_key_hash yet)."""
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("UPDATE tenant_api_keys SET api_key_hash = NULL WHERE tenant_id = %s", (setup["tenant_id"],))
    bf_conn_pepper = PEPPER
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        bf.backfill(c, bf_conn_pepper)  # populate hashes (dark)
    mw = TenantAuthMiddleware(app=None, auth_mode="cloud", db_url=DB_URL)
    resolved = mw._resolve_api_key(setup["api_key"])  # by PLAINTEXT
    assert resolved is not None, "plaintext auth broke after backfill"
    assert resolved[0] == setup["tenant_id"]
