"""Hash-at-rest PR 5: login-drop is UNCONDITIONAL. The server no longer stores the plaintext key, so
an EXISTING account authenticating (login, or a returning Supabase tenant) never receives one — with
GRAFOMEM_API_KEY_LOGIN_DROP set or not; the flag is no longer read. Show-once provisioning (signup,
mint, rotate) is the only path to a usable key, and signup still returns the freshly minted key once.
(Replaces the PR 4 flag tests: "flag off echoes the key" is no longer possible.)
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


@pytest.mark.parametrize("flag", [None, "1", "0"])
def test_login_never_returns_a_key(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("GRAFOMEM_API_KEY_LOGIN_DROP", raising=False)
    else:
        monkeypatch.setenv("GRAFOMEM_API_KEY_LOGIN_DROP", flag)
    pa = _pa()
    email, _tid, _ = _signup(pa)
    result = pa.login(email=email, password="password123")
    assert result is not None
    info, token = result
    assert token, "login must still issue a session JWT"
    assert not info.get("api_key"), "login must NOT carry a plaintext key (none is stored any more)"


def test_link_or_create_existing_tenant_never_returns_a_key(monkeypatch):
    monkeypatch.delenv("GRAFOMEM_API_KEY_LOGIN_DROP", raising=False)
    pa = _pa()
    uid = f"sub-{uuid.uuid4().hex[:12]}"
    email = f"ld-sso-{uuid.uuid4().hex[:8]}@example.com"
    first = pa.ensure_tenant(supabase_uid=uid, email=email, name="SSO Org")
    assert first["api_key"], "first provision (new tenant) is show-once — key returned"
    again = pa.ensure_tenant(supabase_uid=uid, email=email, name="SSO Org")
    assert not again.get("api_key"), "returning-tenant link must NOT carry a key"
    assert again["tenant_id"] == first["tenant_id"]


def test_signup_returns_key_once():
    pa = _pa()
    _email, _tid, info = _signup(pa)
    assert info.get("api_key"), "signup is first-provision show-once — key returned"
    assert info["api_key"].startswith("gfm_")
