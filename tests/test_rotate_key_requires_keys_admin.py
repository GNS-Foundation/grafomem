"""Own-tenant destructive admin-plane routes must require `keys:admin` (2026-10-01).

`require_platform_or_self` (scopes.py:199-200) returns as soon as the caller acts on its OWN tenant — no scope,
no role check. So before this fix ANY own-tenant key, including a `read_only` drafter key or an `agent`
integration key, could call:

  * `POST /v1/cloud/tenants/{own}/rotate-key`  → `DELETE FROM tenant_api_keys WHERE tenant_id` (routes.py:254),
    i.e. delete every key the tenant has and receive the only replacement (lock-out + key hand-over);
  * `POST /v1/cloud/billing/cancel/{own}`      → cancel the tenant's subscription.

Fix: `require_scope(request, "keys:admin")` after the self-access guard on both routes. `keys:admin` is in
TENANT_ADMIN_SCOPES (birth/admin keys keep working); `*` passes every non-platform scope (platform keys keep
working); `read_only` and `agent` carry no `keys:admin` → 403 `Insufficient scope. Required: keys:admin`.

Must-fail discipline: the four negative tests FAIL on main before the fix (rotate-key returns 200 from the mock
TM; cancel returns 503 "Stripe billing not configured" — i.e. the scope check is absent and the route proceeds)
and pass after it. No DB: the caller's TenantContext is injected, as in test_platform_admin_isolation.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aml.cloud.routes import router as cloud_router
from aml.server.scopes import ROLE_SCOPES, TENANT_ADMIN_SCOPES

OWN = "tenant-own"
SCOPE_ERROR = "Insufficient scope. Required: keys:admin"   # verbatim: scopes.py:134 with scope="keys:admin"


class _MockTM:
    """Just enough for rotate-key: the route executes a DELETE on a conn and mints via create_api_key."""
    def __init__(self):
        self.deleted_for: list[str] = []

    def create_api_key(self, tenant_id, name, role="admin", scopes=None, **kw):
        return {"api_key": "gfm_ROTATED_" + tenant_id, "key_id": "k", "role": role, "scopes": scopes}

    def _get_conn(self):
        tm = self

        class _C:
            def execute(self, sql, params=(), *a, **k):
                if sql.startswith("DELETE FROM tenant_api_keys"):
                    tm.deleted_for.append(params[0])
                return self
        return _C()


def _client(role: str, scopes: list[str]) -> tuple[TestClient, _MockTM]:
    app = FastAPI()
    tm = _MockTM()
    app.state.tenant_manager = tm
    # no app.state.stripe_billing: billing/cancel must 403 on scope BEFORE it reaches the 503 "not configured"

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.tenant = SimpleNamespace(tenant_id=OWN, role=role, scopes=list(scopes))
        return await call_next(request)

    app.include_router(cloud_router)
    return TestClient(app, raise_server_exceptions=True), tm


def _read_only():
    return _client("read_only", ROLE_SCOPES["read_only"])


def _agent():
    return _client("agent", ROLE_SCOPES["agent"])


def _admin():
    return _client("admin", TENANT_ADMIN_SCOPES)


def _platform_star():
    return _client("admin", ["*"])


# ── non-vacuity: the fixtures carry what they claim ───────────────────────────────────────────────────────
def test_fixture_roles_do_not_carry_keys_admin():
    assert "keys:admin" not in ROLE_SCOPES["read_only"]
    assert "keys:admin" not in ROLE_SCOPES["agent"]
    assert "keys:admin" in TENANT_ADMIN_SCOPES


# ── rotate-key: negative (FAIL on main: 200, every key deleted) ──────────────────────────────────────────
@pytest.mark.parametrize("mk", [_read_only, _agent], ids=["read_only", "agent"])
def test_own_tenant_rotate_key_requires_keys_admin(mk):
    c, tm = mk()
    r = c.post(f"/v1/cloud/tenants/{OWN}/rotate-key")
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": SCOPE_ERROR}, r.text
    assert tm.deleted_for == [], "the DELETE ran before the scope check — keys were wiped"


# ── billing/cancel: negative (FAIL on main: 503 — the route proceeds past the guard) ─────────────────────
@pytest.mark.parametrize("mk", [_read_only, _agent], ids=["read_only", "agent"])
def test_own_tenant_billing_cancel_requires_keys_admin(mk):
    c, _ = mk()
    r = c.post(f"/v1/cloud/billing/cancel/{OWN}")
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": SCOPE_ERROR}, r.text


# ── positive controls: admin and platform keys are unaffected ───────────────────────────────────────────
@pytest.mark.parametrize("mk", [_admin, _platform_star], ids=["admin_TENANT_ADMIN_SCOPES", "platform_star"])
def test_admin_and_platform_keys_still_rotate(mk):
    c, tm = mk()
    r = c.post(f"/v1/cloud/tenants/{OWN}/rotate-key")
    assert r.status_code == 200, r.text
    assert r.json()["new_api_key"] == "gfm_ROTATED_" + OWN
    assert tm.deleted_for == [OWN]


@pytest.mark.parametrize("mk", [_admin, _platform_star], ids=["admin_TENANT_ADMIN_SCOPES", "platform_star"])
def test_admin_and_platform_keys_pass_cancel_scope(mk):
    c, _ = mk()
    r = c.post(f"/v1/cloud/billing/cancel/{OWN}")
    # past the scope check: the next gate is the (unconfigured) Stripe service, not the scope error
    assert r.status_code == 503, r.text
    assert r.json() == {"detail": "Stripe billing not configured"}
