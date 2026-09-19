"""POST /v1/governed/{decisions,outcomes/bulk,reviews/bulk} require the `governed:write` scope.

Must-fail per route: a `cgr:read`-only key is rejected 403 "Insufficient scope. Required:
governed:write". Positive control: a `*` admin key passes the scope gate (not 403). Bodies are
valid so the request reaches the in-handler scope check (an invalid body would 422 first).

Before the gate (require_scope added to these routes) the cgr:read key was ACCEPTED — the
governed POSTs had no scope check — so these assertions fail on pre-gate code.
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from aml.server.app import create_app
from aml.server.auth import TenantAuthMiddleware
from aml.cloud.tenant_manager import TenantManager

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")

# (path, valid body) — bodies parse so the request reaches the handler's require_scope().
ROUTES = [
    ("/v1/governed/decisions", {"decision": "certify", "invoice_id": "inv-1"}),
    ("/v1/governed/outcomes/bulk", [{"invoice_ref": "inv-1", "outcome": "paid"}]),
    ("/v1/governed/reviews/bulk", [{"invoice_ref": "inv-1", "reviewer_handle": "r1", "rating": 0.5}]),
    # Adjacent governed-write routes (siblings of the three above), gated in the same PR:
    ("/v1/governed/outcomes", {"invoice_ref": "inv-1", "outcome": "paid"}),
    ("/v1/governed/reviews", {"invoice_ref": "inv-1", "reviewer_handle": "r1", "rating": 0.5}),
    ("/v1/governed/verify-batch", {"invoices": []}),
    ("/v1/cgr/rotation", {"prev_key": "a" * 64, "new_key": "b" * 64, "sig": "c" * 128}),
]


@pytest.fixture(scope="module")
def app_instance():
    os.environ["GRAFOMEM_DB_URL"] = DB_URL
    os.environ["GRAFOMEM_AUTH_MODE"] = "cloud"
    os.environ.setdefault("GRAFOMEM_SIGNING_KEY", "b" * 64)
    return create_app(db_url=DB_URL)


@pytest.fixture(scope="module")
def client(app_instance):
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


@pytest.fixture(scope="module")
def keys():
    tm = TenantManager(DB_URL)
    tm.ensure_schema()
    info = tm.create_tenant(name=f"gw-{uuid.uuid4().hex[:8]}")
    cgr = tm.create_api_key(info.id, name="cgr", role="agent", scopes=["cgr:read"])
    adm = tm.create_api_key(info.id, name="adm", role="admin", scopes=["*"])
    return {"cgr": cgr["api_key"], "admin": adm["api_key"]}


@pytest.mark.parametrize("path,body", ROUTES)
def test_cgr_read_key_denied_governed_write(client, keys, path, body):
    r = client.post(path, json=body, headers={"X-API-Key": keys["cgr"]})
    assert r.status_code == 403, f"{path}: expected 403, got {r.status_code} ({r.text[:200]})"
    assert r.json().get("detail") == "Insufficient scope. Required: governed:write", \
        f"{path}: wrong denial message: {r.text[:200]}"


@pytest.mark.parametrize("path,body", ROUTES)
def test_admin_key_passes_scope_gate(client, keys, path, body):
    # Positive control: a `*` admin key must NOT be denied by the scope gate. It may 200 or hit
    # downstream business logic, but it must not be the governed:write 403 — otherwise the gate
    # is over-broad / the test vacuous.
    r = client.post(path, json=body, headers={"X-API-Key": keys["admin"]})
    assert not (r.status_code == 403 and "governed:write" in r.text), \
        f"{path}: admin key wrongly denied by the scope gate ({r.status_code} {r.text[:200]})"
