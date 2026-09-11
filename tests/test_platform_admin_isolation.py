"""P0 — cross-tenant privilege-escalation regression tests (2026-09-11).

Before the fix, every tenant's default key carried role='admin' + scopes=['*'], and
the platform-admin routes gated on `_require_admin` + require_scope('admin:platform'),
which '*' satisfied. So ANY tenant's key could list/create/rotate/read-usage for OTHER
tenants — and `list_tenants` returned every tenant's `api_key`.

These tests pin the fix:
  (a) a fresh (non-platform) tenant key calling list_tenants  → 403
  (b) rotating ANOTHER tenant's key from a non-platform key   → 403
  (c) a PLATFORM-operator key (PLATFORM_TENANT_IDS)           → 200
  + platform routes never return api_key, and they write an audit row.

(a) and (b) FAIL on main (they returned 200) and pass after the fix — that is the
must-fail discipline: watch them fail first.
"""
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aml.cloud.routes import router as cloud_router
from aml.server.scopes import require_platform, require_platform_or_self
from fastapi import HTTPException

PLATFORM_TID = "platform-op-1"
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


@pytest.fixture(autouse=True)
def _platform_env(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_IDS", PLATFORM_TID)


class _Limits:
    max_memories = 1
    max_stores = 1
    max_requests_per_minute = 1


class _Tenant:
    def __init__(self, tid, name="n"):
        self.id = tid
        self.name = name
        self.api_key = "gfm_SECRET_" + tid  # must NEVER appear in list/get responses
        self.plan = "starter"
        self.created_at = datetime.now(timezone.utc)
        self.limits = _Limits()


class _MockTM:
    def list_tenants(self):
        return [_Tenant(TENANT_A), _Tenant(TENANT_B)]

    def get_tenant(self, tid):
        return _Tenant(tid)

    def create_tenant(self, name, plan="starter"):
        return _Tenant("new-" + name, name)

    def create_api_key(self, tenant_id, name, role="admin", scopes=None, **kw):
        return {"api_key": "gfm_ROTATED_" + tenant_id}

    def _get_conn(self):
        class _C:
            def execute(self, *a, **k):
                return self
        return _C()


class _MockAudit:
    def __init__(self):
        self.rows = []

    def log(self, tenant_id, actor, action, resource, metadata=None):
        self.rows.append({"tenant_id": tenant_id, "actor": actor, "action": action})


def _client(caller_tid, scopes=("*",)):
    """A test app that injects a caller TenantContext (default: the over-privileged
    '*'-scoped default key that made the escalation possible)."""
    app = FastAPI()
    app.state.tenant_manager = _MockTM()
    app.state.audit_logger = _MockAudit()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.tenant = SimpleNamespace(
            tenant_id=caller_tid, role="admin", scopes=list(scopes),
        )
        return await call_next(request)

    app.include_router(cloud_router)
    return TestClient(app, raise_server_exceptions=True)


# ── (a) fresh tenant key cannot list tenants ─────────────────────────────────
def test_fresh_tenant_cannot_list_tenants():
    r = _client(TENANT_A).get("/v1/cloud/tenants")
    assert r.status_code == 403, r.text  # FAILS on main (was 200 via '*')


# ── (b) fresh tenant key cannot rotate another tenant's key ──────────────────
def test_fresh_tenant_cannot_rotate_other_tenant():
    r = _client(TENANT_A).post(f"/v1/cloud/tenants/{TENANT_B}/rotate-key")
    assert r.status_code == 403, r.text  # FAILS on main (was 200 via '*')


# ── (c) platform operator key succeeds ───────────────────────────────────────
def test_platform_operator_can_list_tenants():
    c = _client(PLATFORM_TID)
    r = c.get("/v1/cloud/tenants")
    assert r.status_code == 200, r.text


# ── addition (1): list/get responses never carry api_key ─────────────────────
def test_list_tenants_never_returns_api_key():
    r = _client(PLATFORM_TID).get("/v1/cloud/tenants")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body, "expected tenant rows"
    for row in body:
        assert "api_key" not in row, f"api_key leaked in list_tenants row: {row}"


def test_get_tenant_never_returns_api_key():
    # platform operator reads another tenant → 200, but no key
    r = _client(PLATFORM_TID).get(f"/v1/cloud/tenants/{TENANT_A}")
    assert r.status_code == 200, r.text
    assert "api_key" not in r.json()


# ── addition (2): platform route writes an audit row ─────────────────────────
def test_platform_route_writes_audit_row():
    app_client = _client(PLATFORM_TID)
    # reach into the app's mock audit logger
    audit = app_client.app.state.audit_logger
    app_client.get("/v1/cloud/tenants")
    actions = [r["action"] for r in audit.rows]
    assert "list_tenants" in actions, f"no audit row written: {audit.rows}"
    row = next(r for r in audit.rows if r["action"] == "list_tenants")
    assert row["actor"] == PLATFORM_TID


# ── own-tenant self-exception (unit level, no route plumbing) ────────────────
def _fake_request(tid):
    return SimpleNamespace(state=SimpleNamespace(
        tenant=SimpleNamespace(tenant_id=tid, role="admin", scopes=["*"])))


def test_require_platform_rejects_non_platform(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_IDS", PLATFORM_TID)
    with pytest.raises(HTTPException) as e:
        require_platform(_fake_request(TENANT_A))
    assert e.value.status_code == 403


def test_require_platform_allows_platform(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_IDS", PLATFORM_TID)
    require_platform(_fake_request(PLATFORM_TID))  # no raise


def test_require_platform_or_self_allows_own_tenant(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_IDS", PLATFORM_TID)
    require_platform_or_self(_fake_request(TENANT_A), TENANT_A)  # own → no raise


def test_require_platform_or_self_rejects_cross_tenant(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_IDS", PLATFORM_TID)
    with pytest.raises(HTTPException) as e:
        require_platform_or_self(_fake_request(TENANT_A), TENANT_B)
    assert e.value.status_code == 403
