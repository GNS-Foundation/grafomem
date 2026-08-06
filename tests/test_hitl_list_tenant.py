"""Phase 2 — regression: GET /v1/hitl/requests must filter by the REAL tenant, not None.

require_scope() returns None (it only raises 403 on a missing scope). Using its return as the
tenant_id made the query `WHERE tenant_id = NULL`, so the HITL queue always returned 0 rows —
the bug the live smoke exposed. This asserts the list captures the tenant from the auth context.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aml.cloud.hitl_routes import create_hitl_router


class _Ctx:
    def __init__(self, tid):
        self.tenant_id = tid
        self.scopes = ["*"]


class _Cur:
    def __init__(self, conn, q, p):
        self.conn, self.q, self.p = conn, q, p

    def fetchall(self):
        if "FROM hitl_approval_requests" in self.q and "WHERE tenant_id" in self.q:
            tid, status = self.p
            self.conn.captured["tenant_id"] = tid       # record what the query filtered by
            if tid == "corp" and status == "pending":
                return [{"request_id": "r1", "workflow_id": "propose:OUT-1:d", "step_id": "d",
                         "action": "send_email", "resource": "Ana Ruiz",
                         "issued_at": None, "expires_at": None, "status": "pending"}]
        return []


class _Conn:
    def __init__(self):
        self.captured = {}

    def execute(self, q, p=()):
        return _Cur(self, q, p)


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def connection(self):
        c = self.conn

        class _Ctxm:
            def __enter__(self_):
                return c

            def __exit__(self_, *a):
                return False

        return _Ctxm()


def _client(pool, tenant_id):
    router = create_hitl_router(pool, object(), object())
    app = FastAPI()

    @app.middleware("http")
    async def _set_tenant(request, call_next):
        request.state.tenant = _Ctx(tenant_id)
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def test_list_requests_filters_by_real_tenant_not_none():
    pool = _Pool()
    client = _client(pool, "corp")
    r = client.get("/v1/hitl/requests")
    assert r.status_code == 200, r.text
    assert pool.conn.captured.get("tenant_id") == "corp"   # NOT None (the bug)
    assert len(r.json()["requests"]) == 1
    assert r.json()["requests"][0]["action"] == "send_email"
