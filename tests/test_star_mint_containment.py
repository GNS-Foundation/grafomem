"""`*`-mint containment (the *-birth-key class).

- admin-by-omission is unrepresentable: `create_api_key(role="admin")` with no scopes raises.
- `*` is reachable ONLY via an explicit `scopes=["*"]` through `validate_scopes`.
- a legacy empty `{}` + `role="admin"` row resolves at auth time to TENANT_ADMIN_SCOPES (own-tenant
  admin), NEVER `*` — so a scope outside TENANT_ADMIN_SCOPES (e.g. calibration:write) is not satisfied.

Must-fails are shown failing first against pre-fix code (see the PR); positive controls prove `*` is
still reachable deliberately.
"""
import os
import uuid

import psycopg
import pytest

from aml.cloud.tenant_manager import TenantManager
from aml.server.auth import TenantAuthMiddleware
from aml.server.scopes import TENANT_ADMIN_SCOPES

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


@pytest.fixture(scope="module")
def tm():
    m = TenantManager(DB_URL)
    m.ensure_schema()
    return m


@pytest.fixture
def tenant(tm):
    return tm.create_tenant(name=f"star-{uuid.uuid4().hex[:8]}")


def _resolver() -> TenantAuthMiddleware:
    return TenantAuthMiddleware(app=None, auth_mode="cloud", db_url=DB_URL)


def _insert_key(tenant_id: str, api_key: str, role: str, scopes: list[str]) -> None:
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute(
            "INSERT INTO tenant_api_keys (key_id, tenant_id, api_key, name, role, scopes) "
            "VALUES (%s,%s,%s,'t',%s,%s)",
            (uuid.uuid4().hex, tenant_id, api_key, role, scopes))


def test_role_scopes_has_no_admin_star_entry():
    """Regression guard: `admin` must not be an implicit `['*']` in ROLE_SCOPES (the source of the
    `{}`→`*` birth-key class). `*` is reachable only via an explicit scopes=['*']."""
    from aml.server.scopes import ROLE_SCOPES
    assert "admin" not in ROLE_SCOPES


def test_admin_by_omission_raises(tenant, tm):
    """MUST-FAIL: role='admin' with no scopes is refused (no more `{}`→`*` by omission)."""
    with pytest.raises(ValueError, match="explicit scopes"):
        tm.create_api_key(tenant.id, name="adm", role="admin")


def test_explicit_star_mints_star(tenant, tm):
    """POSITIVE CONTROL: a deliberate superuser via the explicit-list path still mints `*`."""
    info = tm.create_api_key(tenant.id, name="su", role="admin", scopes=["*"])
    assert "*" in info["scopes"]


def test_empty_admin_row_resolves_bounded_not_star(tenant):
    """MUST-FAIL (+ containment): a stored `{}` + admin row (the legacy portal/SSO birth) resolves to
    TENANT_ADMIN_SCOPES at auth time, never `*`; a scope outside that set is not satisfied."""
    k = f"gfm_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"
    _insert_key(tenant.id, k, "admin", [])  # empty {} + admin
    resolved = _resolver()._resolve_api_key(k)
    assert resolved is not None
    _tid, _role, scopes, *_ = resolved
    assert "*" not in scopes, "empty {}+admin must NOT resolve to superuser"
    assert set(scopes) == set(TENANT_ADMIN_SCOPES), "must resolve to own-tenant admin"
    assert "calibration:write" not in scopes  # a scope outside TENANT_ADMIN_SCOPES is not granted


def test_explicit_star_row_resolves_star(tenant):
    """POSITIVE CONTROL: an explicitly stored `{*}` key still resolves to `*`."""
    k = f"gfm_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"
    _insert_key(tenant.id, k, "admin", ["*"])
    resolved = _resolver()._resolve_api_key(k)
    assert resolved is not None
    _tid, _role, scopes, *_ = resolved
    assert scopes == ["*"]
