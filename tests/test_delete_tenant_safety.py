"""B7: scripts/delete_tenant.py safety.

(1) The platform guard must not be vacuous: with PLATFORM_TENANT_IDS unset or empty in the local
environment the script refuses to run at all — dry-run AND --live — with "export it first".
(2) Orphan probe: the dry-run reports, per tenant, the row counts in decision_records, audit_logs,
gcrumbs_breadcrumbs, execution_receipts and every other tenant-scoped table outside the walk; --live
REFUSES (exit 4, before any write) when any of them is non-zero. (3) A clean tenant still tears down.

MUST-FAIL on main 9caa995: an unset PLATFORM_TENANT_IDS lets the dry-run proceed; a tenant with one
decision_records row is torn down on --live (the row stays orphaned); no orphan counts are shown.
Runs against the local test database through the script's own main() (sys.argv patched).
"""
import importlib.util
import os
import pathlib
import sys
import uuid

import psycopg
import pytest

from aml.cloud.tenant_manager import TenantManager

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("delete_tenant", _ROOT / "scripts/delete_tenant.py")
dt = importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(dt)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GRAFOMEM_MIGRATE_URL", DB_URL)
    monkeypatch.delenv("GRAFOMEM_ROTATE_DB_URL", raising=False)
    monkeypatch.setenv("PLATFORM_TENANT_IDS", "platform-" + uuid.uuid4().hex[:8])   # some OTHER tenant
    TenantManager(DB_URL).ensure_schema()
    with psycopg.connect(DB_URL, autocommit=True) as c:   # the probe tables the tests insert into
        c.execute("CREATE TABLE IF NOT EXISTS decision_records (decision_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
                  "store_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), query TEXT NOT NULL, "
                  "retrieved_refs JSONB NOT NULL DEFAULT '[]', model_id TEXT, raw_output TEXT)")


def _run(monkeypatch, capsys, *argv) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["delete_tenant.py", *argv])
    rc = dt.main()
    return rc, capsys.readouterr().out


def _tenant() -> str:
    t = TenantManager(DB_URL).create_tenant(name=f"b7-{uuid.uuid4().hex[:8]}")
    return t.id


def _exists(table: str, col: str, val: str) -> bool:
    with psycopg.connect(DB_URL) as c:
        return c.execute(f"SELECT 1 FROM {table} WHERE {col} = %s", (val,)).fetchone() is not None


# ---------------------------------------------------------------- 1. PLATFORM_TENANT_IDS must be set

@pytest.mark.parametrize("mode", ["dry-run", "live"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_refuses_when_platform_tenant_ids_is_unset_or_empty(env, monkeypatch, capsys, mode, value):
    if value is None:
        monkeypatch.delenv("PLATFORM_TENANT_IDS")
    else:
        monkeypatch.setenv("PLATFORM_TENANT_IDS", value)
    tid = _tenant()
    rc, out = _run(monkeypatch, capsys, tid, *(["--live"] if mode == "live" else []))
    assert rc != 0, out
    assert "PLATFORM_TENANT_IDS" in out and "export it first" in out, out
    assert "DRY-RUN" not in out and "LIVE" not in out, "nothing may run before the guard is in place"
    assert _exists("tenants", "id", tid), "nothing written"


# ---------------------------------------------------------------- 2. orphan probe: shown on dry-run, refused on --live

def test_tenant_with_a_decision_record_is_refused_on_live_and_nothing_is_written(env, monkeypatch, capsys):
    tid = _tenant()
    key_id = TenantManager(DB_URL).create_api_key(tid, name="k", role="agent", scopes=["cgr:read"])["key_id"]
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("INSERT INTO decision_records (decision_id, tenant_id, store_id, query, model_id, raw_output) "
                  "VALUES (%s, %s, 's', 'q', 'm', 'o')", (f"b7-{uuid.uuid4().hex[:8]}", tid))
    # dry-run shows the orphan count
    rc, out = _run(monkeypatch, capsys, tid)
    assert rc == 0, out
    assert "decision_records" in out and "rows=1" in out.split("decision_records", 1)[1][:40], out
    assert "orphan" in out.lower(), out
    # --live refuses with exit 4 before any write
    rc, out = _run(monkeypatch, capsys, tid, "--live")
    assert rc == 4, out
    assert "REFUSED" in out and "decision_records" in out, out
    assert _exists("tenants", "id", tid) and _exists("tenant_api_keys", "key_id", key_id) \
        and _exists("decision_records", "tenant_id", tid), "a refused --live must write nothing"


def test_dry_run_lists_the_other_tenant_scoped_tables(env, monkeypatch, capsys):
    tid = _tenant()
    rc, out = _run(monkeypatch, capsys, tid)
    assert rc == 0, out
    for table in ("decision_records", "audit_logs", "gcrumbs_breadcrumbs", "execution_receipts"):
        assert table in out, f"{table} missing from the orphan probe: {out}"


# ---------------------------------------------------------------- 3. a clean tenant still tears down (control)

def test_clean_tenant_tears_down(env, monkeypatch, capsys):
    tid = _tenant()
    key_id = TenantManager(DB_URL).create_api_key(tid, name="k", role="agent", scopes=["cgr:read"])["key_id"]
    rc, out = _run(monkeypatch, capsys, tid)
    assert rc == 0 and "DRY-RUN" in out, out
    assert _exists("tenants", "id", tid)
    rc, out = _run(monkeypatch, capsys, tid, "--live")
    assert rc == 0 and "DONE" in out, out
    assert not _exists("tenants", "id", tid) and not _exists("tenant_api_keys", "key_id", key_id)
