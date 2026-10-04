"""An EXISTING account authenticating (login, or a returning Supabase tenant) never receives an API
key: the server stores no plaintext (hash-at-rest; held 018b dropped the column), so there is nothing
to echo. Show-once provisioning (signup, mint, rotate) is the only path to a usable key, and signup
still returns the freshly minted key once. (B4: the login-drop environment flag that once gated this
is gone; the behaviour is unconditional.)
"""
import os
import uuid

import pytest

TEST_DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


def _pa():
    pytest.importorskip("bcrypt")
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


def test_login_never_returns_a_key():
    pa = _pa()
    email, _tid, _ = _signup(pa)
    result = pa.login(email=email, password="password123")
    assert result is not None
    info, token = result
    assert token, "login must still issue a session JWT"
    assert not info.get("api_key"), "login must NOT carry a plaintext key (none is stored any more)"


def test_link_or_create_existing_tenant_never_returns_a_key():
    pa = _pa()
    uid = f"sub-{uuid.uuid4().hex[:12]}"
    email = f"ld-sso-{uuid.uuid4().hex[:8]}@example.com"
    first = pa.ensure_tenant(supabase_uid=uid, email=email, name="SSO Org")
    assert first["api_key"], "first provision (new tenant) is show-once — key returned"
    again = pa.ensure_tenant(supabase_uid=uid, email=email, name="SSO Org")
    assert not again.get("api_key"), "returning-tenant link must NOT carry a key"
    assert again["tenant_id"] == first["tenant_id"]


def test_link_existing_tenant_by_email_never_returns_a_key():
    """Legacy account (email/password) later signing in through Supabase: linked by email, no key."""
    pa = _pa()
    email, tid, _ = _signup(pa)
    linked = pa.ensure_tenant(supabase_uid=f"sub-{uuid.uuid4().hex[:12]}", email=email, name="LD Test")
    assert linked["tenant_id"] == tid
    assert not linked.get("api_key"), "email-linked existing tenant must NOT carry a key"


def test_signup_returns_key_once():
    pa = _pa()
    _email, _tid, info = _signup(pa)
    assert info.get("api_key"), "signup is first-provision show-once — key returned"
    assert info["api_key"].startswith("gfm_")
