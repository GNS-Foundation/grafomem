"""PortalAuth.change_password — re-verify current, validate + set new (bcrypt)."""
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
    email = f"cpw-{uuid.uuid4().hex[:10]}@example.com"
    info, _ = pa.signup(name="CPW Test", email=email, password="oldpassword1")
    return email, info["tenant_id"]


def test_change_password_happy_path():
    pa = _pa()
    email, tid = _signup(pa)
    pa.change_password(tid, "oldpassword1", "newpassword2")
    # old no longer works, new does
    assert pa.login(email=email, password="oldpassword1") is None
    assert pa.login(email=email, password="newpassword2") is not None


def test_wrong_current_rejected():
    pa = _pa()
    email, tid = _signup(pa)
    with pytest.raises(ValueError, match="[Cc]urrent password is incorrect"):
        pa.change_password(tid, "WRONGcurrent", "newpassword2")
    assert pa.login(email=email, password="oldpassword1") is not None   # unchanged


def test_new_too_short_rejected():
    pa = _pa()
    _, tid = _signup(pa)
    with pytest.raises(ValueError, match="at least 8"):
        pa.change_password(tid, "oldpassword1", "short")


def test_new_same_as_current_rejected():
    pa = _pa()
    _, tid = _signup(pa)
    with pytest.raises(ValueError, match="differ"):
        pa.change_password(tid, "oldpassword1", "oldpassword1")


def test_no_password_account_rejected():
    # SSO/managed account: a tenant row with NULL password_hash cannot change here.
    pa = _pa()
    conn = pa._get_conn()
    tid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO tenants (id, name, api_key, plan, email, password_hash, status) "
        "VALUES (%s,%s,%s,'starter',%s, NULL, 'active')",
        (tid, "SSO User", "gfm_" + uuid.uuid4().hex, f"sso-{tid[:8]}@example.com"))
    with pytest.raises(ValueError, match="[Nn]o password"):
        pa.change_password(tid, "whatever1", "newpassword2")


# ── update_profile (editable display name) ──
def test_update_profile_changes_name():
    pa = _pa()
    _, tid = _signup(pa)
    out = pa.update_profile(tid, name="  Renamed Org  ")
    assert out == {"name": "Renamed Org"}          # trimmed
    row = pa._get_conn().execute("SELECT name FROM tenants WHERE id=%s", (tid,)).fetchone()
    assert row["name"] == "Renamed Org"


def test_update_profile_rejects_empty():
    pa = _pa()
    _, tid = _signup(pa)
    with pytest.raises(ValueError, match="1 and 120"):
        pa.update_profile(tid, name="   ")


def test_update_profile_rejects_too_long():
    pa = _pa()
    _, tid = _signup(pa)
    with pytest.raises(ValueError, match="1 and 120"):
        pa.update_profile(tid, name="x" * 121)


# ── update_profile timezone ──
def test_update_profile_sets_valid_timezone():
    pa = _pa()
    _, tid = _signup(pa)
    out = pa.update_profile(tid, name="Org", timezone="America/New_York")
    assert out == {"name": "Org", "timezone": "America/New_York"}
    row = pa._get_conn().execute("SELECT timezone FROM tenants WHERE id=%s", (tid,)).fetchone()
    assert row["timezone"] == "America/New_York"


def test_update_profile_rejects_invalid_timezone():
    pa = _pa()
    _, tid = _signup(pa)
    with pytest.raises(ValueError, match="[Ii]nvalid timezone"):
        pa.update_profile(tid, name="Org", timezone="Mars/Phobos")


def test_update_profile_empty_timezone_clears():
    pa = _pa()
    _, tid = _signup(pa)
    pa.update_profile(tid, name="Org", timezone="Europe/Madrid")
    pa.update_profile(tid, name="Org", timezone="")   # clear
    row = pa._get_conn().execute("SELECT timezone FROM tenants WHERE id=%s", (tid,)).fetchone()
    assert row["timezone"] is None


def test_update_profile_name_only_leaves_timezone():
    pa = _pa()
    _, tid = _signup(pa)
    pa.update_profile(tid, name="Org", timezone="Asia/Tokyo")
    pa.update_profile(tid, name="Renamed")            # timezone omitted ⇒ unchanged
    row = pa._get_conn().execute("SELECT name, timezone FROM tenants WHERE id=%s", (tid,)).fetchone()
    assert row["name"] == "Renamed" and row["timezone"] == "Asia/Tokyo"
