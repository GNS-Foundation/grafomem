"""Per-key revoke — DELETE /v1/portal/api-keys/{key_id} (Option B).

The per-key counterpart to /rotate-key (which deletes ALL keys). Must-fail first (shown RED on current
main, which has no such route):
  - a revoked key stops authenticating on the next request (row deleted + auth cache evicted);
  - another tenant's key_id → 404 (tenant-scoped; never touches or reveals it).
Positive control: revoking one own key leaves the tenant's other keys intact and authenticating.
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from aml.cloud.tenant_manager import TenantManager
from aml.server.app import create_app
from aml.server.auth import TenantAuthMiddleware

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


@pytest.fixture(scope="module")
def app_instance():
    os.environ["GRAFOMEM_DB_URL"] = DB_URL
    os.environ["AUTH_MODE"] = "cloud"
    os.environ["GRAFOMEM_AUTH_MODE"] = "cloud"
    os.environ.setdefault("GRAFOMEM_SIGNING_KEY", "b" * 64)
    return create_app(db_url=DB_URL)


@pytest.fixture(scope="module")
def client(app_instance):
    with TestClient(app_instance) as c:
        _disable_auth_cache(app_instance)
        yield c


@pytest.fixture(scope="module")
def tm():
    m = TenantManager(DB_URL)
    m.ensure_schema()
    return m


def _auth_middleware(app) -> TenantAuthMiddleware:
    node = getattr(app, "middleware_stack", None)
    for _ in range(50):
        if node is None:
            break
        if isinstance(node, TenantAuthMiddleware):
            return node
        node = getattr(node, "app", None)
    raise AssertionError("TenantAuthMiddleware not found — the 60s auth cache would still be live")


def _disable_auth_cache(app) -> None:
    mw = _auth_middleware(app)
    mw._api_key_cache.clear()
    mw._cache_ttl = 0


@pytest.fixture
def live_cache(app_instance):
    """Restore the REAL 60s auth-cache TTL — the cache-on test measures the eviction, not the DB path."""
    mw = _auth_middleware(app_instance)
    mw._api_key_cache.clear()
    mw._cache_ttl = 60
    yield mw
    mw._api_key_cache.clear()
    mw._cache_ttl = 0


def _key_authenticates(client, api_key: str) -> bool:
    return client.get("/v1/stores", headers={"X-API-Key": api_key}).status_code == 200


def _signup(client):
    email = f"revoke-{uuid.uuid4().hex[:10]}@example.test"
    r = client.post("/v1/portal/signup",
                    json={"email": email, "password": "correct-horse-battery-staple", "name": "revoke"})
    assert r.status_code == 201, r.text
    body = r.json()
    return {"auth": {"Authorization": f"Bearer {body['token']}"}, "api_key": body["api_key"]}


def _mint(client, auth, name="second") -> dict:
    r = client.post("/v1/portal/api-keys", headers=auth, json={"name": name, "role": "admin"})
    assert r.status_code == 200, r.text
    return r.json()  # {api_key, key_id, ...}


def _list_key_ids(client, auth) -> set[str]:
    r = client.get("/v1/portal/me", headers=auth)
    assert r.status_code == 200, r.text
    return {k["key_id"] for k in (r.json().get("keys") or [])}


# ── must-fail 1: a revoked key stops authenticating; the survivor keeps working ──

def test_revoke_removes_key_and_stops_auth(client):
    acct = _signup(client)
    held = acct["api_key"]                       # birth key (survivor)
    second = _mint(client, acct["auth"])         # the key we will revoke
    assert _key_authenticates(client, held) and _key_authenticates(client, second["api_key"])

    r = client.delete(f"/v1/portal/api-keys/{second['key_id']}", headers=acct["auth"])
    assert r.status_code == 200, r.text          # RED on main (no such route → 404)

    assert not _key_authenticates(client, second["api_key"]), "revoked key must stop authenticating"
    assert _key_authenticates(client, held), "the survivor key must still authenticate"


# ── must-fail 2: another tenant's key_id → 404, and that key survives ──

def test_revoke_other_tenants_key_is_404_and_survives(client):
    a = _signup(client)
    b = _signup(client)
    b_key_ids = _list_key_ids(client, b["auth"])
    b_target = next(iter(b_key_ids))

    r = client.delete(f"/v1/portal/api-keys/{b_target}", headers=a["auth"])
    assert r.status_code == 404, r.text
    assert "not found for this tenant" in r.text, "must be the tenant-scoped 404, not a bare missing-route 404"
    assert _key_authenticates(client, b["api_key"]), "B's key must be untouched by A's failed revoke"


# ── positive control: revoke one own key, the rest of the list survives ──

def test_revoke_survivors_intact(client):
    acct = _signup(client)
    held = acct["api_key"]
    k2 = _mint(client, acct["auth"], name="k2")
    k3 = _mint(client, acct["auth"], name="k3")
    before = _list_key_ids(client, acct["auth"])
    assert {k2["key_id"], k3["key_id"]} <= before and len(before) == 3

    r = client.delete(f"/v1/portal/api-keys/{k2['key_id']}", headers=acct["auth"])
    assert r.status_code == 200, r.text

    after = _list_key_ids(client, acct["auth"])
    assert k2["key_id"] not in after, "revoked key must be gone from the list"
    assert {k3["key_id"]} <= after, "the other minted key must survive"
    assert _key_authenticates(client, held) and _key_authenticates(client, k3["api_key"])
    assert not _key_authenticates(client, k2["api_key"])


# ── cache-on: revoke must EVICT the live 60s cache, not only the DB row ──
# Runs with the REAL 60s TTL. Deleting the row is not revocation on its own — a key used once keeps
# authenticating for up to 60s unless the endpoint calls invalidate_tenant_key_cache. Must-fail =
# comment out that eviction in revoke_api_key → the revoked key still authenticates here.

def test_revoke_evicts_auth_cache(client, live_cache):
    acct = _signup(client)
    second = _mint(client, acct["auth"])
    assert _key_authenticates(client, second["api_key"]), "precondition: the key must work"
    assert second["api_key"] in live_cache._api_key_cache, (
        "precondition: the key must actually be CACHED, or this test proves nothing"
    )

    r = client.delete(f"/v1/portal/api-keys/{second['key_id']}", headers=acct["auth"])
    assert r.status_code == 200, r.text

    assert not _key_authenticates(client, second["api_key"]), (
        "the revoked key still authenticates from the 60s cache — DELETE /api-keys/{id} is not "
        "evicting the tenant's cached keys (invalidate_tenant_key_cache)"
    )
