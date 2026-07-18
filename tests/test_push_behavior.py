import pytest
import time
from datetime import datetime
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from typing import Optional

from aml.cloud.push_routes import router as push_router
from aml.cloud.push_service import PushDispatchService

class MockDbConn:
    def __init__(self, executed=None):
        self.executed = executed if executed is not None else []
    def execute(self, query, params=()):
        self.executed.append((query, params))

class MockDbPool:
    def __init__(self):
        self._conn = MockDbConn()
    class _Ctx:
        def __init__(self, conn):
            self._conn = conn
        def __enter__(self):
            return self._conn
        def __exit__(self, *a):
            pass
    def connection(self):
        return self._Ctx(self._conn)

def _make_key():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    return priv, pub_hex

def _sign_challenge(priv, approver_id: str, timestamp_str: str) -> str:
    challenge = f"grafomem.push.register.v1:{approver_id}:{timestamp_str}".encode("utf-8")
    return priv.sign(challenge).hex()

@pytest.fixture
def app_client():
    app = FastAPI()
    app.include_router(push_router)
    with TestClient(app) as client:
        yield client

# 1. /push/register 401 on bad/stale signature
def test_push_register_401_bad_signature(app_client):
    priv, pub_hex = _make_key()
    ts = str(int(time.time() * 1000))
    # Sign a modified challenge
    wrong_challenge = f"grafomem.push.register.v1:{pub_hex}:{ts}9".encode("utf-8")
    sig = priv.sign(wrong_challenge).hex()
    
    resp = app_client.post("/v1/push/register", json={
        "approver_id": pub_hex,
        "platform": "ios",
        "push_token": "dummy_token"
    }, headers={
        "x-gns-signature": sig,
        "x-gns-timestamp": ts
    })
    assert resp.status_code == 401
    assert "Invalid signature" in resp.json()["detail"]

def test_push_register_401_stale_timestamp(app_client):
    priv, pub_hex = _make_key()
    stale_ts = str(int(time.time() * 1000) - 120_000)
    sig = _sign_challenge(priv, pub_hex, stale_ts)
    
    resp = app_client.post("/v1/push/register", json={
        "approver_id": pub_hex,
        "platform": "ios",
        "push_token": "dummy_token"
    }, headers={
        "x-gns-signature": sig,
        "x-gns-timestamp": stale_ts
    })
    assert resp.status_code == 401
    assert "stale or skewed" in resp.json()["detail"].lower()

# 2. rate limit for /v1/push/register
def test_push_register_rate_limit(app_client):
    priv, pub_hex = _make_key()
    
    # We clear the rate limit dict for test isolation
    from aml.cloud.push_routes import _rate_limits
    _rate_limits.clear()

    # The limit is 5 per 60 seconds
    for _ in range(5):
        ts = str(int(time.time() * 1000))
        sig = _sign_challenge(priv, pub_hex, ts)
        
        # It passes rate limiter and hits later logic (could be 500 due to no DB),
        # but we only care that it doesn't return 429.
        resp = app_client.post("/v1/push/register", json={
            "approver_id": pub_hex,
            "platform": "ios",
            "push_token": "dummy"
        }, headers={
            "x-gns-signature": sig,
            "x-gns-timestamp": ts
        })
        assert resp.status_code != 429

    # The 6th request should be 429
    ts = str(int(time.time() * 1000))
    sig = _sign_challenge(priv, pub_hex, ts)
    resp = app_client.post("/v1/push/register", json={
        "approver_id": pub_hex,
        "platform": "ios",
        "push_token": "dummy"
    }, headers={
        "x-gns-signature": sig,
        "x-gns-timestamp": ts
    })
    assert resp.status_code == 429
    assert "Too many requests" in resp.json()["detail"]

# 3. dispatch-failure isolation (request creation survives APNs down)
# 4. prune behavior (410 -> deleted, 400 -> not deleted)
class MockAPNsResponse:
    def __init__(self, status_code, reason=""):
        self.status_code = status_code
        self._reason = reason
    def json(self):
        return {"reason": self._reason}

def test_prune_behavior_410_deletes_400_survives():
    pool = MockDbPool()
    svc = PushDispatchService(db_pool=pool)
    svc.env = "sandbox"
    
    with patch.object(svc.client, "post") as mock_post:
        # 1. 410 Unregistered should DELETE the token
        mock_post.return_value = MockAPNsResponse(410, "Unregistered")
        svc._send_push("dummy_token", 1, "req", datetime.now())
        
        executed = pool._conn.executed
        assert len(executed) == 1
        query, params = executed[0]
        assert "DELETE FROM approver_push_tokens" in query
        assert params[0] == "dummy_token"

        pool._conn.executed.clear()

        # 2. 400 BadDeviceToken should NOT DELETE
        mock_post.return_value = MockAPNsResponse(400, "BadDeviceToken")
        svc._send_push("dummy_token_2", 1, "req", datetime.now())
        
        assert len(pool._conn.executed) == 0

def test_dispatch_failure_isolation():
    pool = MockDbPool()
    svc = PushDispatchService(db_pool=pool)
    
    with patch.object(svc, "_send_push", side_effect=Exception("APNs is down")):
        # Should not raise exception
        svc._dispatch_sync("tenant", "req_id", datetime.now())

