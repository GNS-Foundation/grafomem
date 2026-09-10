"""Rotating a tenant's API key must revoke the OLD key — on BOTH rotation paths.

Two routes rotate a key and they do not agree:

  * portal  `POST /v1/portal/rotate-key`            (portal_routes.py:398)
      DELETE tenant_api_keys -> create_api_key -> **UPDATE tenants SET api_key**
  * admin   `POST /v1/cloud/tenants/{id}/rotate-key` (routes.py:203)
      DELETE tenant_api_keys -> create_api_key      (no legacy sync)

`server/auth.py:126-133` falls back UNCONDITIONALLY to `tenants.api_key` when the
`tenant_api_keys` lookup misses. So after an ADMIN rotation the stale
`tenants.api_key` still resolves — and the legacy branch hands back role `admin`
with no scopes, no allowed_stores, no ip_allowlist and no expiry, i.e. a BROADER
and non-expiring identity than the key it replaced.

NOTE — the 60-second auth cache is a SEPARATE, UNFIXED defect.
`TenantAuthMiddleware._api_key_cache` has a 60s TTL and
`TenantAuthMiddleware.invalidate_cache` (auth.py:94) has **zero callers** in the
tree — neither rotation path invalidates it. A key used once therefore keeps
authenticating for up to 60s after ANY rotation, regardless of the database. That
window is not what these tests measure, so they neutralise the TTL explicitly
(`_cache_ttl = 0`) and fail loudly if the middleware instance cannot be found.
Neutralising it here is what makes these tests measure the DB path; it is not a
claim that the window is closed.
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


def _disable_auth_cache(app) -> None:
    """Set the auth middleware's TTL to 0 so these tests measure the DB, not the cache.

    Walks the built middleware chain. Raises if the instance is not found — a
    silent miss would leave the 60s cache in play and could make a broken fix
    look tested.
    """
    node = getattr(app, "middleware_stack", None)
    seen = 0
    while node is not None and seen < 50:
        if isinstance(node, TenantAuthMiddleware):
            node._api_key_cache.clear()
            node._cache_ttl = 0
            return
        node = getattr(node, "app", None)
        seen += 1
    raise AssertionError(
        "TenantAuthMiddleware not found in the middleware chain — the 60s auth "
        "cache would still be live and these tests would not measure the DB path."
    )


def _key_authenticates(client, api_key: str) -> bool:
    """True iff `api_key` is accepted by the auth middleware."""
    return client.get("/v1/stores", headers={"X-API-Key": api_key}).status_code == 200


@pytest.fixture(scope="module")
def tm():
    m = TenantManager(DB_URL)
    m.ensure_schema()
    return m


def test_probe_discriminates(client):
    """Non-vacuity: the probe must reject a key that was never issued."""
    assert not _key_authenticates(client, "gm_" + uuid.uuid4().hex * 2)


def test_admin_rotation_revokes_old_key(client, tm):
    info = tm.create_tenant(name=f"rot-admin-{uuid.uuid4().hex[:8]}")
    old_key = info.api_key
    assert _key_authenticates(client, old_key), "precondition: the original key must work"

    r = client.post(
        f"/v1/cloud/tenants/{info.id}/rotate-key",
        headers={"X-API-Key": old_key},
    )
    assert r.status_code == 200, r.text
    new_key = r.json()["new_api_key"]

    assert _key_authenticates(client, new_key), "the rotated-in key must work"
    assert not _key_authenticates(client, old_key), (
        "REVOCATION BYPASS: the old key still authenticates after an admin-side "
        "rotation — tenants.api_key was not synced and auth.py falls back to it"
    )


def test_portal_rotation_revokes_old_key(client, tm):
    email = f"rot-portal-{uuid.uuid4().hex[:8]}@example.test"
    r = client.post(
        "/v1/portal/signup",
        json={"email": email, "password": "correct-horse-battery-staple", "name": "rot-portal"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    auth = {"Authorization": f"Bearer {body['token']}"}
    old_key = body["api_key"]
    assert _key_authenticates(client, old_key), "precondition: the original key must work"

    r = client.post("/v1/portal/rotate-key", headers=auth)
    assert r.status_code == 200, r.text
    new_key = r.json()["api_key"]

    assert _key_authenticates(client, new_key), "the rotated-in key must work"
    assert not _key_authenticates(client, old_key), (
        "the old key still authenticates after a portal-side rotation"
    )
