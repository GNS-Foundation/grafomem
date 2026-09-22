"""Hash-at-rest PR 3.5 — mint-time api_key_hash (best-effort NULL when the pepper is unset).

Must-fail (shown failing on current main first): a minted key's api_key_hash == HMAC(pepper, key) and
it resolves via HASH under dual-read with NO PLAINTEXT-PATH line. On pre-3.5 code the mint leaves
api_key_hash NULL → the key resolves via plaintext (a PLAINTEXT-PATH line). Best-effort: pepper unset
→ NULL + a WARNING (key_id), never a failed mint.
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
from aml.server.api_key_hash import compute_api_key_hash, PEPPER_ENV

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
PEPPER = "onmint-pepper-" + "f" * 40


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
    return tm.create_tenant(name=f"onmint-{uuid.uuid4().hex[:8]}")


def _hash_of(tenant_id, api_key):
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        r = c.execute("SELECT api_key_hash FROM tenant_api_keys WHERE tenant_id=%s AND api_key=%s",
                      (tenant_id, api_key)).fetchone()
    return r["api_key_hash"] if r else "MISSING"


def test_create_api_key_populates_hash(tenant, tm, monkeypatch):
    """create_api_key (also the rotate + console create-key path) writes api_key_hash = HMAC(pepper,key)."""
    monkeypatch.setenv(PEPPER_ENV, PEPPER)
    info = tm.create_api_key(tenant.id, name="minted", role="agent", scopes=["cgr:read"])
    stored = _hash_of(tenant.id, info["api_key"])
    assert stored is not None and stored != "MISSING"
    assert bytes(stored) == compute_api_key_hash(info["api_key"], PEPPER)


def test_create_tenant_birth_key_populates_hash(tm, monkeypatch):
    """The create_tenant birth key is hashed on mint too (an additional path covered)."""
    monkeypatch.setenv(PEPPER_ENV, PEPPER)
    info = tm.create_tenant(name=f"birth-{uuid.uuid4().hex[:8]}")
    stored = _hash_of(info.id, info.api_key)
    assert stored is not None and stored != "MISSING"
    assert bytes(stored) == compute_api_key_hash(info.api_key, PEPPER)


def test_mint_null_when_pepper_unset_logs_warning(tenant, tm, monkeypatch, caplog):
    """Best-effort: pepper unset → api_key_hash NULL, mint SUCCEEDS, and a WARNING names the key_id."""
    monkeypatch.delenv(PEPPER_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger="grafomem.auth"):
        info = tm.create_api_key(tenant.id, name="nopepper", role="agent", scopes=["cgr:read"])
    assert _hash_of(tenant.id, info["api_key"]) is None, "no pepper → NULL hash (not a failed mint)"
    assert any("NULL api_key_hash" in r.getMessage() and info["key_id"] in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING), "NULL fallback must WARN with key_id"


def test_minted_key_resolves_via_hash_under_dual_read(tenant, tm, monkeypatch):
    """MUST-FAIL vs pre-3.5: a key minted WITH the pepper resolves via the HASH path under dual-read
    (no PLAINTEXT-PATH). On pre-3.5 the mint left NULL → it would resolve via plaintext."""
    monkeypatch.setenv(PEPPER_ENV, PEPPER)
    monkeypatch.setenv("GRAFOMEM_API_KEY_DUAL_READ", "1")
    info = tm.create_api_key(tenant.id, name="dr-minted", role="agent", scopes=["cgr:read"])
    mw = TenantAuthMiddleware(app=None, auth_mode="cloud", db_url=DB_URL)
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        row, via = mw._lookup_key_row(c, info["api_key"])
    assert via == "hash" and row is not None, "a freshly-minted (hashed) key must resolve via hash"
    assert mw.plaintext_path_resolutions == 0
