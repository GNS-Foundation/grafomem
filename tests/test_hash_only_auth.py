"""Hash-at-rest PR 5 — auth resolves API keys by api_key_hash ONLY (replaces test_dual_read_auth.py).

A correct hash resolves (current or retiring pepper); a wrong or NULL hash does NOT resolve — there
is no plaintext fallback and nothing is counted as a plaintext-path resolution. With no pepper
configured nothing resolves.
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
PEPPER = "hashonly-pepper-" + "e" * 40


@pytest.fixture(scope="module")
def tm():
    os.environ["GRAFOMEM_API_KEY_PEPPER"] = PEPPER
    m = TenantManager(DB_URL)
    m.ensure_schema()
    for name in ("017_api_key_hash.sql", "018a_api_key_plaintext_nullable.sql"):
        path = _ROOT / "src/aml/cloud/migrations" / name
        if not path.exists():  # 018a is absent on main before PR 5
            continue
        with psycopg.connect(DB_URL, autocommit=True) as c:
            c.execute(path.read_text())
    return m


@pytest.fixture
def tenant(tm, monkeypatch):
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER", PEPPER)
    monkeypatch.delenv("GRAFOMEM_API_KEY_PEPPER_RETIRING", raising=False)
    return tm.create_tenant(name=f"ho-{uuid.uuid4().hex[:8]}")  # birth key = TENANT_ADMIN_SCOPES


def _set_hash(tenant_id, hashbytes):
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("UPDATE tenant_api_keys SET api_key_hash=%s WHERE tenant_id=%s", (hashbytes, tenant_id))


def _mw():
    return TenantAuthMiddleware(app=None, auth_mode="cloud", db_url=DB_URL)


def _lookup(mw, api_key):
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        return mw._lookup_key_row(c, api_key)


def test_correct_hash_resolves_via_hash(tenant):
    mw = _mw()
    row, via = _lookup(mw, tenant.api_key)
    assert via == "hash" and row is not None
    res = mw._resolve_api_key(tenant.api_key)
    assert res is not None and res[0] == tenant.id
    assert mw.plaintext_path_resolutions == 0


def test_wrong_hash_does_not_resolve_no_plaintext_fallback(tenant):
    _set_hash(tenant.id, os.urandom(32))  # deliberately wrong (and unique) hash
    mw = _mw()
    assert _lookup(mw, tenant.api_key) == (None, None), "a wrong hash must not resolve — there is no plaintext fallback"
    assert mw._resolve_api_key(tenant.api_key) is None
    assert mw.plaintext_path_resolutions == 0


def test_null_hash_does_not_resolve(tenant):
    _set_hash(tenant.id, None)
    mw = _mw()
    assert _lookup(mw, tenant.api_key) == (None, None)


def test_retiring_pepper_resolves_via_hash(tenant, monkeypatch):
    """A key hashed under the RETIRING pepper still resolves via the hash path (rotation window)."""
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER", "new-" + PEPPER)      # current (different)
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER_RETIRING", PEPPER)      # retiring
    _set_hash(tenant.id, compute_api_key_hash(tenant.api_key, PEPPER))  # hashed under the retiring pepper
    row, via = _lookup(_mw(), tenant.api_key)
    assert via == "hash" and row is not None


def test_no_pepper_configured_resolves_nothing(tenant, monkeypatch, caplog):
    monkeypatch.delenv("GRAFOMEM_API_KEY_PEPPER", raising=False)
    monkeypatch.delenv("GRAFOMEM_API_KEY_PEPPER_RETIRING", raising=False)
    import logging
    with caplog.at_level(logging.ERROR, logger="grafomem.auth"):
        assert _lookup(_mw(), tenant.api_key) == (None, None)
    assert any("no GRAFOMEM_API_KEY_PEPPER" in r.getMessage() for r in caplog.records)
