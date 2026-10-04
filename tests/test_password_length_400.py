"""GB10: a password over 72 bytes (UTF-8) is a client error, answered 400 with one clean message on
every password-taking portal route; the login catch-all never echoes exception text.

MUST-FAIL on main 0a5ebf9: there login reaches bcrypt 5 with an over-long password and the route's
catch-all turns bcrypt's ValueError into 500 "Login crashed: <exception text>"; signup and
change-password already answer 400 but with bcrypt's internal message; a long password on an
unknown email answers 401 while a known email answers 500 (an account-existence oracle); and a forced
internal error in login echoes its text to the client. Controls (exactly 72 bytes; 36 x "é" = 72
bytes) pass before and after. Through the real app via TestClient against the local test DB.
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from aml.server.app import create_app

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")

CLEAN_PREFIX = "Password must be at most 72 bytes"
LONG_ASCII = "a" * 73          # 73 bytes
LONG_UTF8 = "é" * 40           # 40 characters, 80 bytes UTF-8
OK_72_ASCII = "a" * 72         # 72 bytes: the bcrypt maximum, accepted
OK_72_UTF8 = "é" * 36          # 36 characters, 72 bytes: accepted
BCRYPT_TEXT = "truncate manually"


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("bcrypt")
    os.environ["GRAFOMEM_DB_URL"] = DB_URL
    os.environ["GRAFOMEM_AUTH_MODE"] = "cloud"
    os.environ.setdefault("GRAFOMEM_SIGNING_KEY", "b" * 64)
    os.environ.setdefault("GRAFOMEM_PORTAL_SECRET", "gb10-test-secret-" + "x" * 24)
    return create_app(db_url=DB_URL)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200  # positive control: the app built and serves
        yield c


def _email(tag="gb10"):
    return f"{tag}-{uuid.uuid4().hex[:10]}@example.com"


def _signup(client, password="password123"):
    email = _email()
    r = client.post("/v1/portal/signup", json={"name": "GB10", "email": email, "password": password})
    assert r.status_code == 201, r.text
    return email, r.json()["token"]


def _assert_clean_400(r):
    assert r.status_code == 400, f"{r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert detail.startswith(CLEAN_PREFIX), detail
    assert BCRYPT_TEXT not in r.text


# ---------------------------------------------------------------- login: 500 today → 400

def test_login_long_password_known_email_is_400(client):
    email, _ = _signup(client)
    _assert_clean_400(client.post("/v1/portal/login", json={"email": email, "password": LONG_ASCII}))


def test_login_long_utf8_password_is_400(client):
    """Bytes, not characters: 40 accented characters are 80 bytes of UTF-8."""
    email, _ = _signup(client)
    _assert_clean_400(client.post("/v1/portal/login", json={"email": email, "password": LONG_UTF8}))


def test_login_long_password_same_answer_known_and_unknown_email(client):
    """No 401/400 split: a long password gets the same status and the same detail whether or not
    the email has an account, so the check cannot be used to enumerate accounts."""
    email, _ = _signup(client)
    known = client.post("/v1/portal/login", json={"email": email, "password": LONG_ASCII})
    unknown = client.post("/v1/portal/login", json={"email": _email("nobody"), "password": LONG_ASCII})
    _assert_clean_400(known)
    _assert_clean_400(unknown)
    assert known.json()["detail"] == unknown.json()["detail"]


def test_login_internal_error_is_not_echoed(client, app, monkeypatch):
    """The login catch-all stays a 500 but returns a generic detail; exception text is for the log."""
    def boom(*_a, **_k):
        raise RuntimeError("secret-internal")
    monkeypatch.setattr(app.state.portal_auth, "login", boom)
    r = client.post("/v1/portal/login", json={"email": _email(), "password": "password123"})
    assert r.status_code == 500, f"{r.status_code} {r.text}"
    assert "secret-internal" not in r.text
    assert r.json()["detail"] == "Login failed"


def test_login_other_valueerror_is_a_500_not_a_400(client, app, monkeypatch):
    """Cowork (#198 review): only the password-length refusal is a client error. Any other
    ValueError out of pa.login — e.g. bcrypt.checkpw raising "Invalid salt" on a malformed stored
    hash — is a server fault: 500, generic detail, internal text not echoed."""
    def bad_salt(*_a, **_k):
        raise ValueError("Invalid salt")
    monkeypatch.setattr(app.state.portal_auth, "login", bad_salt)
    r = client.post("/v1/portal/login", json={"email": _email(), "password": "password123"})
    assert r.status_code == 500, f"{r.status_code} {r.text}"
    assert "Invalid salt" not in r.text
    assert r.json()["detail"] == "Login failed"


# ---------------------------------------------------------------- signup / change-password: clean message

def test_signup_long_password_is_400_with_clean_message(client):
    r = client.post("/v1/portal/signup", json={"name": "GB10", "email": _email(), "password": LONG_ASCII})
    _assert_clean_400(r)


def test_signup_long_utf8_password_is_400_with_clean_message(client):
    r = client.post("/v1/portal/signup", json={"name": "GB10", "email": _email(), "password": LONG_UTF8})
    _assert_clean_400(r)


def test_change_password_long_new_password_is_400_with_clean_message(client):
    _, token = _signup(client)
    r = client.post("/v1/portal/change-password",
                    json={"current_password": "password123", "new_password": LONG_ASCII},
                    headers={"Authorization": f"Bearer {token}"})
    _assert_clean_400(r)


def test_change_password_long_current_password_is_400_with_clean_message(client):
    _, token = _signup(client)
    r = client.post("/v1/portal/change-password",
                    json={"current_password": LONG_ASCII, "new_password": "another-password-9"},
                    headers={"Authorization": f"Bearer {token}"})
    _assert_clean_400(r)


# ---------------------------------------------------------------- controls (green before and after)

def test_exactly_72_bytes_is_accepted_everywhere(client):
    email, token = _signup(client, password=OK_72_ASCII)
    assert client.post("/v1/portal/login", json={"email": email, "password": OK_72_ASCII}).status_code == 200
    r = client.post("/v1/portal/change-password",
                    json={"current_password": OK_72_ASCII, "new_password": "b" * 72},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert client.post("/v1/portal/login", json={"email": email, "password": "b" * 72}).status_code == 200


def test_36_accented_characters_72_bytes_is_accepted(client):
    assert len(OK_72_UTF8.encode("utf-8")) == 72
    email, _ = _signup(client, password=OK_72_UTF8)
    assert client.post("/v1/portal/login", json={"email": email, "password": OK_72_UTF8}).status_code == 200


def test_wrong_password_within_limit_is_still_401(client):
    email, _ = _signup(client)
    r = client.post("/v1/portal/login", json={"email": email, "password": "wrong-password-1"})
    assert r.status_code == 401
    assert client.post("/v1/portal/login", json={"email": _email("nobody"), "password": "x" * 12}).status_code == 401
