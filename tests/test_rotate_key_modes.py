"""Unit coverage for the new rotate_all_tenant_keys modes:
  --only-tenant   → mint ALONGSIDE (no delete), 1:1 or fresh-scoped
  --revoke-key    → delete one key by key_id

Runs against local Postgres. Asserts DB state and that no api_key leaks to stdout.
"""
import importlib.util
import io
import os
import pathlib
import uuid
from contextlib import redirect_stdout

import psycopg
import pytest

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")

# Load the script as a module.
_SPEC = importlib.util.spec_from_file_location(
    "rotate_all_tenant_keys",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rotate_all_tenant_keys.py")
rot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rot)


@pytest.fixture
def platform_tenant(monkeypatch):
    from aml.cloud.tenant_manager import TenantManager
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS tenant_api_keys CASCADE")
        c.execute("DROP TABLE IF EXISTS tenants CASCADE")
    TenantManager(DB_URL).ensure_schema()
    tid = uuid.uuid4().hex
    old_key_id = uuid.uuid4().hex
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("INSERT INTO tenants (id, name, plan, status, api_key) "
                  "VALUES (%s,%s,'enterprise','active',%s)", (tid, "platform-ulissy-like", f"gfm_{uuid.uuid4().hex}"))
        c.execute("INSERT INTO tenant_api_keys (key_id, tenant_id, api_key, name, role, scopes) "
                  "VALUES (%s,%s,%s,'Default Admin Key','admin',%s)",
                  (old_key_id, tid, f"gfm_{uuid.uuid4().hex}", ["*"]))
    monkeypatch.setenv("GRAFOMEM_ROTATE_DB_URL", DB_URL)
    monkeypatch.setenv("PLATFORM_TENANT_IDS", tid)
    monkeypatch.delenv("ROTATE_EXCLUDE_TENANT_IDS", raising=False)
    return tid, old_key_id


def _keys(tid):
    with psycopg.connect(DB_URL, autocommit=True) as c:
        return c.execute(
            "SELECT key_id, role, scopes, name FROM tenant_api_keys WHERE tenant_id=%s ORDER BY created_at",
            (tid,)).fetchall()


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rot.main(argv)
    out = buf.getvalue()
    assert "gfm_" not in out, "a key value leaked to stdout"
    return out


def test_only_tenant_dryrun_writes_nothing(platform_tenant):
    tid, _ = platform_tenant
    before = _keys(tid)
    out = _run(["--only-tenant", tid])
    assert "DRY-RUN" in out
    assert _keys(tid) == before  # untouched


def test_only_tenant_mints_alongside_preserving_scopes(platform_tenant, tmp_path):
    tid, old_key_id = platform_tenant
    _run(["--only-tenant", tid, "--live", "--out", str(tmp_path / "k.jsonl")])
    rows = _keys(tid)
    assert len(rows) == 2, "should mint alongside (old kept + new)"
    ids = {r[0] for r in rows}
    assert old_key_id in ids, "old key must NOT be deleted (coexistence)"
    new = [r for r in rows if r[0] != old_key_id][0]
    assert new[1] == "admin" and new[2] == ["*"], "new key preserves role + {*} scopes 1:1"


def test_only_tenant_fresh_scoped_mint(platform_tenant, tmp_path):
    tid, old_key_id = platform_tenant
    _run(["--only-tenant", tid, "--scopes", "cgr:read", "--name", "ci-weekly-refresh",
          "--role", "agent", "--live", "--out", str(tmp_path / "k.jsonl")])
    rows = _keys(tid)
    assert len(rows) == 2
    new = [r for r in rows if r[0] != old_key_id][0]
    assert new[1] == "agent" and new[2] == ["cgr:read"] and new[3] == "ci-weekly-refresh", \
        f"fresh scoped key wrong: {new}"


def test_scoped_mint_requires_name(platform_tenant):
    tid, _ = platform_tenant
    with pytest.raises(SystemExit):
        _run(["--only-tenant", tid, "--scopes", "cgr:read", "--live"])


def test_revoke_key_deletes_one_row(platform_tenant, tmp_path):
    tid, old_key_id = platform_tenant
    # mint a second key so revoking the old one does not strand the tenant
    _run(["--only-tenant", tid, "--live", "--out", str(tmp_path / "k.jsonl")])
    assert len(_keys(tid)) == 2
    _run(["--revoke-key", old_key_id, "--live"])
    rows = _keys(tid)
    assert len(rows) == 1 and rows[0][0] != old_key_id, "revoke should delete exactly the named key"


def test_revoke_dryrun_keeps_row(platform_tenant):
    tid, old_key_id = platform_tenant
    out = _run(["--revoke-key", old_key_id])
    assert "DRY-RUN" in out
    assert any(r[0] == old_key_id for r in _keys(tid))
