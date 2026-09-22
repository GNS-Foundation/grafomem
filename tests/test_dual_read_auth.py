"""Hash-at-rest PR 3 — dual-read auth behind GRAFOMEM_API_KEY_DUAL_READ (DARK by default).

Flag on: resolve by api_key_hash first (HMAC current pepper, then retiring), plaintext fallback on a
hash miss — each plaintext-path resolution logged + counted (the 7-day zero-plaintext-window signal).
No writes to the plaintext column. Flag off (default): legacy plaintext-only, no dual-read.
"""
import os
import pathlib
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from aml.cloud.tenant_manager import TenantManager
from aml.server.auth import TenantAuthMiddleware
from aml.server.api_key_hash import compute_api_key_hash

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
PEPPER = "dualread-pepper-" + "e" * 40


def _apply_017():
    sql = (_ROOT / "src/aml/cloud/migrations/017_api_key_hash.sql").read_text()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute(sql)


@pytest.fixture(scope="module")
def tm():
    m = TenantManager(DB_URL)
    m.ensure_schema()
    _apply_017()
    return m


@pytest.fixture
def tenant(tm):
    return tm.create_tenant(name=f"dr-{uuid.uuid4().hex[:8]}")  # birth key = TENANT_ADMIN_SCOPES


def _set_hash(tenant_id, api_key, hashbytes):
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("UPDATE tenant_api_keys SET api_key_hash=%s WHERE tenant_id=%s AND api_key=%s",
                  (hashbytes, tenant_id, api_key))


def _mw():
    return TenantAuthMiddleware(app=None, auth_mode="cloud", db_url=DB_URL)


def _lookup(mw, api_key):
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        return mw._lookup_key_row(c, api_key)


def test_correct_hash_resolves_via_hash_not_plaintext(tenant, monkeypatch):
    """POSITIVE CONTROL: correct api_key_hash → resolves via the HASH path; plaintext untouched."""
    monkeypatch.setenv("GRAFOMEM_API_KEY_DUAL_READ", "1")
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER", PEPPER)
    key = tenant.api_key
    _set_hash(tenant.id, key, compute_api_key_hash(key, PEPPER))  # correct hash
    mw = _mw()
    row, via = _lookup(mw, key)
    assert via == "hash" and row is not None
    before = mw.plaintext_path_resolutions
    res = mw._resolve_api_key(key)
    assert res is not None and res[0] == tenant.id
    assert mw.plaintext_path_resolutions == before, "correct hash must NOT touch the plaintext path"


def test_wrong_hash_not_via_hash_and_visible_as_plaintext_path(tenant, monkeypatch):
    """MUST-FAIL: a row with a deliberately WRONG api_key_hash must NOT resolve via hash, and the
    resolution must be VISIBLE as a plaintext-path fallback (counted + logged)."""
    monkeypatch.setenv("GRAFOMEM_API_KEY_DUAL_READ", "1")
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER", PEPPER)
    key = tenant.api_key
    _set_hash(tenant.id, key, os.urandom(32))  # deliberately wrong (and unique) hash
    mw = _mw()
    row, via = _lookup(mw, key)
    assert via != "hash", "a wrong hash must NOT resolve via the hash path"
    assert via == "plaintext" and row is not None, "must fall back to plaintext (request still succeeds)"
    before = mw.plaintext_path_resolutions
    res = mw._resolve_api_key(key)
    assert res is not None and res[0] == tenant.id
    assert mw.plaintext_path_resolutions == before + 1, "wrong hash must be counted as a plaintext-path resolution"


def test_flag_off_is_legacy_plaintext_no_dualread(tenant, monkeypatch):
    """Flag off (default): legacy plaintext-only; a wrong hash is irrelevant and nothing is counted."""
    monkeypatch.delenv("GRAFOMEM_API_KEY_DUAL_READ", raising=False)
    key = tenant.api_key
    _set_hash(tenant.id, key, os.urandom(32))  # wrong (unique) hash — ignored when flag off
    mw = _mw()
    row, via = _lookup(mw, key)
    assert via == "plaintext" and row is not None
    before = mw.plaintext_path_resolutions
    res = mw._resolve_api_key(key)
    assert res is not None and res[0] == tenant.id
    assert mw.plaintext_path_resolutions == before, "flag off: no dual-read, no plaintext-path counter"


def test_retiring_pepper_resolves_via_hash(tenant, monkeypatch):
    """A key hashed under the RETIRING pepper still resolves via the hash path (rotation window)."""
    monkeypatch.setenv("GRAFOMEM_API_KEY_DUAL_READ", "1")
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER", "new-" + PEPPER)      # current (different)
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER_RETIRING", PEPPER)      # retiring
    key = tenant.api_key
    _set_hash(tenant.id, key, compute_api_key_hash(key, PEPPER))  # hashed under the retiring pepper
    mw = _mw()
    row, via = _lookup(mw, key)
    assert via == "hash" and row is not None, "retiring-pepper hash must still resolve via hash"
