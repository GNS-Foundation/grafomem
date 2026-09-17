"""014 step (a): GET /v1/portal/me must expose NO usable credential.

Stronger than "no api_key field": walk EVERY string in the response body (nested
objects and lists included) and try each as X-API-Key against a protected endpoint.
Every attempt must be rejected (401/403); a renamed field that still leaks the key
must also fail. Positive control: a freshly minted tenant_api_keys key must return 200
on the same endpoint, so a broken endpoint can't make the test pass vacuously.

Fails BEFORE the fix (today /me returns tenant["api_key"] verbatim, which authenticates).
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from aml.server.app import create_app
from aml.server.auth import TenantAuthMiddleware

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
PROTECTED = "/v1/usage/current"


@pytest.fixture(scope="module")
def app_instance():
    os.environ["GRAFOMEM_DB_URL"] = DB_URL
    os.environ["GRAFOMEM_AUTH_MODE"] = "cloud"
    os.environ.setdefault("GRAFOMEM_SIGNING_KEY", "b" * 64)
    return create_app(db_url=DB_URL)


@pytest.fixture(scope="module")
def client(app_instance):
    # Neutralise the 60s auth cache so probes measure the DB, not a cached miss.
    node = getattr(app_instance, "middleware_stack", None)
    for _ in range(50):
        if node is None:
            break
        if isinstance(node, TenantAuthMiddleware):
            node._api_key_cache.clear(); node._cache_ttl = 0
            break
        node = getattr(node, "app", None)
    with TestClient(app_instance) as c:
        yield c


def _all_strings(obj):
    """Every string value anywhere in a JSON structure (recursive)."""
    out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_all_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_all_strings(v))
    return out


def _authenticates(client, value: str) -> bool:
    return client.get(PROTECTED, headers={"X-API-Key": value}).status_code == 200


def test_portal_me_exposes_no_usable_credential(client):
    email = f"me-leak-{uuid.uuid4().hex[:8]}@example.test"
    r = client.post("/v1/portal/signup",
                    json={"email": email, "password": "correct-horse-battery-staple", "name": "me-leak"})
    assert r.status_code == 201, r.text
    body = r.json()
    minted_key = body["api_key"]        # the one-time mint reveal (signup response)
    auth = {"Authorization": f"Bearer {body['token']}"}

    # Positive control: the freshly minted key MUST authenticate — otherwise the probe
    # is vacuous (a broken endpoint would make every check "pass").
    assert _authenticates(client, minted_key), (
        f"positive control failed: a freshly minted key did not authenticate on {PROTECTED}"
    )

    me = client.get("/v1/portal/me", headers=auth)
    assert me.status_code == 200, me.text
    leaks = [s for s in _all_strings(me.json()) if _authenticates(client, s)]
    assert leaks == [], (
        f"/v1/portal/me leaked a usable credential: {len(leaks)} string(s) in the body "
        f"authenticated against {PROTECTED}"
    )

    # Cheap secondary check (NOT the acceptance): no flat api_key field.
    assert "api_key" not in me.json(), "/me still has a flat api_key field"


def test_portal_me_lists_key_metadata(client):
    """014 step (a): /me must actually LIST the tenant's key metadata (>=1 row).

    Regression for the pooled-connection proxy bug: the /me reads must bind the
    _PooledConnectionProxy to a local before fetch. Inlined as
    pa._get_conn().execute(...).fetchall() the proxy is GC'd right after execute(),
    returning+resetting the connection mid-query, so fetchall() fails and the silent
    except yields an empty keys[] even though the tenant has a key. The test harness
    builds a real RoutingPool, so this path is exercised.
    """
    email = f"me-keys-{uuid.uuid4().hex[:8]}@example.test"
    r = client.post("/v1/portal/signup",
                    json={"email": email, "password": "correct-horse-battery-staple", "name": "me-keys"})
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['token']}"}

    me = client.get("/v1/portal/me", headers=auth)
    assert me.status_code == 200, me.text
    keys = me.json().get("keys")
    assert isinstance(keys, list) and len(keys) >= 1, (
        f"/me returned no key metadata (keys={keys!r}) — the pooled read likely GC'd "
        f"its connection proxy before fetch"
    )
    k0 = keys[0]
    assert k0.get("key_id") and k0.get("role"), f"key metadata incomplete: {k0!r}"


def test_login_returns_working_key_from_tenant_api_keys(client):
    """014 step (a) regression: signup/login still hand the console a USABLE key.

    tenants.api_key is no longer written, so login/auto_provision must source the key
    from tenant_api_keys — otherwise they return NULL and the console (which persists the
    login response as its working key) breaks. Also proves the key did NOT come from
    tenants.api_key: that column must be NULL for a post-014 signup.
    """
    email = f"login-key-{uuid.uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"
    r = client.post("/v1/portal/signup",
                    json={"email": email, "password": password, "name": "login-key"})
    assert r.status_code == 201, r.text
    tenant_id = r.json()["tenant_id"]

    # Log in fresh (the path the console uses on every sign-in).
    lr = client.post("/v1/portal/login", json={"email": email, "password": password})
    assert lr.status_code == 200, lr.text
    login_key = lr.json().get("api_key")
    assert login_key, "login returned no api_key — the console would lose its credential"
    assert _authenticates(client, login_key), (
        "login's api_key did not authenticate — login must source the key from tenant_api_keys"
    )

    # The key must NOT have come from tenants.api_key: that column is NULL post-014.
    import psycopg
    with psycopg.connect(DB_URL) as conn:
        row = conn.execute(
            "SELECT api_key FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
        assert row is not None
        col = row[0] if isinstance(row, tuple) else row["api_key"]
        assert col is None, f"tenants.api_key should be NULL post-014, got {col!r}"
