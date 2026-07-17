"""
Tests for the HITL approver inbox endpoint:

    GET /v1/hitl/approvers/{approver_id}/requests

Covers the signed-challenge auth gate (missing headers, stale timestamp,
bad signature), authorization (a valid signature from a non-approver key is
rejected 403), and the happy path (an active approver receives only their
tenants' pending, non-expired requests). Mirrors test_hitl_unauthorized.py.
"""
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from aml.cloud.hitl_routes import create_hitl_router


# --- Mocks -----------------------------------------------------------------

class MockOrchestrator:
    def resume_workflow(self, workflow_id: str, approved: bool):
        pass


class MockGcrumbs:
    def append_breadcrumb(self, tenant_id, event_type, payload, conn=None):
        pass


class MockCursor:
    def __init__(self, parent, query, params):
        self.parent = parent
        self.query = query
        self.params = params

    def fetchall(self):
        q = self.query
        if "FROM hitl_approvers" in q:
            approver_id = self.params[0]
            return [
                {"tenant_id": t}
                for t in self.parent.approver_tenants.get(approver_id, [])
            ]
        if "FROM hitl_approval_requests" in q:
            now_dt, tenant_ids = self.params[0], self.params[1]
            rows = [
                r for r in self.parent.requests
                if r["status"] == "pending"
                and r["expires_at"] > now_dt
                and r["tenant_id"] in tenant_ids
            ]
            rows.sort(key=lambda r: r["issued_at"], reverse=True)
            return rows[:50]
        return []

    def fetchone(self):
        return None


class MockDbConn:
    def __init__(self, approver_tenants, requests):
        self.approver_tenants = approver_tenants
        self.requests = requests

    def execute(self, query, params=()):
        return MockCursor(self, query, params)


class MockDbPool:
    def __init__(self, approver_tenants, requests):
        self._conn = MockDbConn(approver_tenants, requests)

    class _Ctx:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self._conn

        def __exit__(self, *a):
            pass

    def connection(self):
        return self._Ctx(self._conn)


# --- Helpers ---------------------------------------------------------------

def _make_key():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    return priv, pub_hex


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _sign_challenge(priv, approver_id: str, timestamp_str: str) -> str:
    challenge = f"grafomem.hitl.inbox.v1:{approver_id}:{timestamp_str}".encode("utf-8")
    return priv.sign(challenge).hex()


def _client(approver_tenants=None, requests=None) -> TestClient:
    pool = MockDbPool(approver_tenants or {}, requests or [])
    router = create_hitl_router(pool, MockOrchestrator(), MockGcrumbs())
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# --- Tests -----------------------------------------------------------------

def test_inbox_missing_headers_401():
    _, pub_hex = _make_key()
    client = _client(approver_tenants={pub_hex: ["tenant-1"]})
    resp = client.get(f"/v1/hitl/approvers/{pub_hex}/requests")
    assert resp.status_code == 401


def test_inbox_stale_timestamp_401():
    priv, pub_hex = _make_key()
    client = _client(approver_tenants={pub_hex: ["tenant-1"]})
    stale = str(_now_ms() - 120_000)  # 2 minutes old; window is 60s
    resp = client.get(
        f"/v1/hitl/approvers/{pub_hex}/requests",
        headers={
            "X-GNS-Signature": _sign_challenge(priv, pub_hex, stale),
            "X-GNS-Timestamp": stale,
        },
    )
    assert resp.status_code == 401
    assert "tamp" in resp.json()["detail"].lower()  # "Stale or future timestamp"


def test_inbox_bad_signature_401():
    priv, pub_hex = _make_key()
    client = _client(approver_tenants={pub_hex: ["tenant-1"]})
    ts = str(_now_ms())
    # Sign a DIFFERENT challenge than the header timestamp claims.
    wrong_sig = _sign_challenge(priv, pub_hex, ts + "9")
    resp = client.get(
        f"/v1/hitl/approvers/{pub_hex}/requests",
        headers={"X-GNS-Signature": wrong_sig, "X-GNS-Timestamp": ts},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid signature"


def test_inbox_non_approver_403():
    # Valid key, valid fresh signature — but NOT a registered approver.
    priv, pub_hex = _make_key()
    client = _client(approver_tenants={})  # nobody is an approver
    ts = str(_now_ms())
    resp = client.get(
        f"/v1/hitl/approvers/{pub_hex}/requests",
        headers={
            "X-GNS-Signature": _sign_challenge(priv, pub_hex, ts),
            "X-GNS-Timestamp": ts,
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Not an active approver"


def test_inbox_valid_approver_returns_only_scoped_pending():
    priv, pub_hex = _make_key()
    now = datetime.now(timezone.utc)
    requests = [
        {  # returned
            "request_id": "req-visible", "action": "inference",
            "resource": "model-x", "status": "pending", "tenant_id": "tenant-1",
            "issued_at": now, "expires_at": now + timedelta(hours=1),
        },
        {  # dropped: expired
            "request_id": "req-expired", "action": "inference",
            "resource": "model-x", "status": "pending", "tenant_id": "tenant-1",
            "issued_at": now, "expires_at": now - timedelta(minutes=1),
        },
        {  # dropped: wrong tenant
            "request_id": "req-other", "action": "inference",
            "resource": "model-x", "status": "pending", "tenant_id": "tenant-2",
            "issued_at": now, "expires_at": now + timedelta(hours=1),
        },
        {  # dropped: already decided
            "request_id": "req-approved", "action": "inference",
            "resource": "model-x", "status": "approved", "tenant_id": "tenant-1",
            "issued_at": now, "expires_at": now + timedelta(hours=1),
        },
    ]
    client = _client(approver_tenants={pub_hex: ["tenant-1"]}, requests=requests)
    ts = str(_now_ms())
    resp = client.get(
        f"/v1/hitl/approvers/{pub_hex}/requests",
        headers={
            "X-GNS-Signature": _sign_challenge(priv, pub_hex, ts),
            "X-GNS-Timestamp": ts,
        },
    )
    assert resp.status_code == 200
    ids = [r["request_id"] for r in resp.json()["requests"]]
    assert ids == ["req-visible"]
