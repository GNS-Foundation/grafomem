"""PR 4 (hash-at-rest) login-drop: an EXISTING account authenticating no longer receives its
plaintext api_key when GRAFOMEM_API_KEY_LOGIN_DROP is on. Show-once provisioning (mint / rotate)
becomes the only path to a usable key. First-time provisioning (signup) is unaffected — it still
returns the freshly minted key once.

Flag default OFF preserves the legacy behaviour (login echoes the working key), so merging is dark
and the cutover is a reversible env flip.
"""
import os
import uuid

import pytest

TEST_DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


def _pa():
    try:
        import bcrypt  # noqa: F401
    except Exception:
        pytest.skip("bcrypt not installed")
    from aml.cloud.portal_auth import PortalAuth
    pa = PortalAuth(TEST_DB_URL, secret_key="test-secret")
    try:
        pa.ensure_schema()
    except Exception as e:
        pytest.skip(f"portal schema unavailable: {e}")
    return pa


def _signup(pa):
    email = f"ld-{uuid.uuid4().hex[:10]}@example.com"
    info, _ = pa.signup(name="LD Test", email=email, password="password123")
    return email, info["tenant_id"], info


# ── legacy: flag OFF preserves the working-key echo (backward compatible) ──

def test_login_returns_key_when_flag_off(monkeypatch):
    monkeypatch.delenv("GRAFOMEM_API_KEY_LOGIN_DROP", raising=False)
    pa = _pa()
    email, _tid, _ = _signup(pa)
    result = pa.login(email=email, password="password123")
    assert result is not None
    info, _token = result
    assert info.get("api_key"), "flag off must preserve the legacy login-key echo"
    assert info["api_key"].startswith("gfm_")


# ── login-drop: flag ON removes the plaintext key from an existing-account login ──

def test_login_drops_key_when_flag_on(monkeypatch):
    # MUST-FAIL on current main (login always echoes the key); GREEN once the flag path lands.
    monkeypatch.setenv("GRAFOMEM_API_KEY_LOGIN_DROP", "1")
    pa = _pa()
    email, _tid, _ = _signup(pa)
    result = pa.login(email=email, password="password123")
    assert result is not None
    info, token = result
    assert token, "login must still issue a session JWT"
    assert not info.get("api_key"), "flag on: login must NOT carry a usable plaintext key"


def test_link_or_create_existing_tenant_drops_key_when_flag_on(monkeypatch):
    # The Supabase returning-tenant path is a login too — it must drop the key under the flag.
    pa = _pa()
    uid = f"sub-{uuid.uuid4().hex[:12]}"
    email = f"ld-sso-{uuid.uuid4().hex[:8]}@example.com"
    # First call provisions the tenant (brand-new branch — show-once, key present).
    monkeypatch.setenv("GRAFOMEM_API_KEY_LOGIN_DROP", "1")
    first = pa.ensure_tenant(supabase_uid=uid, email=email, name="SSO Org")
    assert first["api_key"], "first provision (new tenant) is show-once — key returned"
    # Second call is a returning login (supabase_uid match branch) — flag on drops the key.
    again = pa.ensure_tenant(supabase_uid=uid, email=email, name="SSO Org")
    assert not again.get("api_key"), "flag on: returning-tenant link must NOT carry the key"
    assert again["tenant_id"] == first["tenant_id"]


# ── provisioning is unaffected: signup still returns the freshly minted key once ──

def test_signup_returns_key_regardless_of_flag(monkeypatch):
    monkeypatch.setenv("GRAFOMEM_API_KEY_LOGIN_DROP", "1")
    pa = _pa()
    _email, _tid, info = _signup(pa)
    assert info.get("api_key"), "signup is first-provision show-once — key returned even with flag on"
    assert info["api_key"].startswith("gfm_")
