"""Hash-at-rest — mint-time api_key_hash (PR 3.5), hardened by PR 5: the hash is the ONLY credential
stored (api_key NULL), auth resolves by hash only, and a mint with the pepper unset is REFUSED
(PepperMissing) instead of writing a row that could never authenticate.
"""
import logging
import os
import pathlib
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from aml.cloud.tenant_manager import TenantManager
from aml.server.auth import TenantAuthMiddleware
from aml.server.api_key_hash import compute_api_key_hash, PEPPER_ENV, PepperMissing

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
PEPPER = "onmint-pepper-" + "f" * 40


def _apply_017():
    sql = (_ROOT / "src/aml/cloud/migrations/017_api_key_hash.sql").read_text()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute(sql)


@pytest.fixture(scope="module")
def tm():
    os.environ[PEPPER_ENV] = PEPPER
    m = TenantManager(DB_URL)
    m.ensure_schema()
    _apply_017()
    return m


@pytest.fixture
def tenant(tm, monkeypatch):
    monkeypatch.setenv(PEPPER_ENV, PEPPER)
    return tm.create_tenant(name=f"onmint-{uuid.uuid4().hex[:8]}")


def _hash_of(tenant_id, key_id):
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        r = c.execute("SELECT api_key_hash FROM tenant_api_keys WHERE tenant_id=%s AND key_id=%s",
                      (tenant_id, key_id)).fetchone()
    return r["api_key_hash"] if r else "MISSING"


def _birth_key_id(tenant_id):
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        return c.execute("SELECT key_id FROM tenant_api_keys WHERE tenant_id=%s", (tenant_id,)).fetchone()["key_id"]


def test_create_api_key_populates_hash(tenant, tm, monkeypatch):
    """create_api_key (also the rotate + console create-key path) writes api_key_hash = HMAC(pepper,key)."""
    monkeypatch.setenv(PEPPER_ENV, PEPPER)
    info = tm.create_api_key(tenant.id, name="minted", role="agent", scopes=["cgr:read"])
    stored = _hash_of(tenant.id, info["key_id"])
    assert stored is not None and stored != "MISSING"
    assert bytes(stored) == compute_api_key_hash(info["api_key"], PEPPER)


def test_create_tenant_birth_key_populates_hash(tm, monkeypatch):
    """The create_tenant birth key is hashed on mint too (an additional path covered)."""
    monkeypatch.setenv(PEPPER_ENV, PEPPER)
    info = tm.create_tenant(name=f"birth-{uuid.uuid4().hex[:8]}")
    stored = _hash_of(info.id, _birth_key_id(info.id))
    assert stored is not None and stored != "MISSING"
    assert bytes(stored) == compute_api_key_hash(info.api_key, PEPPER)


def test_mint_refused_when_pepper_unset(tenant, tm, monkeypatch, caplog):
    """PR 5: pepper unset → the mint is REFUSED (PepperMissing) and an ERROR names the key; no row."""
    monkeypatch.delenv(PEPPER_ENV, raising=False)
    with caplog.at_level(logging.ERROR, logger="grafomem.auth"):
        with pytest.raises(PepperMissing):
            tm.create_api_key(tenant.id, name="nopepper", role="agent", scopes=["cgr:read"])
    assert any("mint REFUSED" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


def test_minted_key_resolves_via_hash(tenant, tm, monkeypatch):
    """A key minted WITH the pepper resolves via the HASH path (the only path since PR 5)."""
    monkeypatch.setenv(PEPPER_ENV, PEPPER)
    info = tm.create_api_key(tenant.id, name="dr-minted", role="agent", scopes=["cgr:read"])
    mw = TenantAuthMiddleware(app=None, auth_mode="cloud", db_url=DB_URL)
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        row, via = mw._lookup_key_row(c, info["api_key"])
    assert via == "hash" and row is not None, "a freshly-minted (hashed) key must resolve via hash"
    assert mw.plaintext_path_resolutions == 0
