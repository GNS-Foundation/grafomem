"""gcrumbs `/v1/gcrumbs/verify` auth boundary (#4).

The path is method-overloaded:
  * POST — a STATELESS receipt verifier (no DB): legitimately anonymous.
  * GET  — a DB-backed chain verification that reads the CALLER'S tenant.

The blanket path-based auth-skip covered BOTH, so the GET ran with no tenant context
and fell back to the "default" namespace — silently verifying the wrong (empty) chain
instead of the caller's (it reported 0/N for every real tenant). These tests lock the
method-scoped exemption: GET requires auth (⇒ caller's tenant, no arbitrary-tenant
read), POST stays public.
"""
import os

import pytest
from fastapi.testclient import TestClient

from aml.server.app import create_app


def _client() -> TestClient:
    db = os.environ.get("GRAFOMEM_DB_URL")
    if not db:
        pytest.skip("GRAFOMEM_DB_URL not set")
    from aml.backends.postgres_gmp import PostgresGMPBackend
    app = create_app(
        backend_factory=lambda: PostgresGMPBackend(db),
        db_url=db,
        auth_mode="token",
        tokens={"test-token": "tenant1"},
    )
    return TestClient(app)


def test_get_chain_verify_requires_auth():
    # No auth header ⇒ the DB-backed GET chain-verify must be REJECTED (401), not run
    # anonymously under "default". If this passes without a key, the #4 bug is back.
    resp = _client().get("/v1/gcrumbs/verify")
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"


def test_get_chain_verify_runs_under_caller_tenant_when_authed():
    # WITH a valid key ⇒ reaches the route under the caller's tenant (tenant1), not
    # "default". A fresh tenant legitimately has no chain ⇒ status "empty"/"intact",
    # but crucially NOT 401 and NOT the anonymous "default" path.
    resp = _client().get("/v1/gcrumbs/verify", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    assert "status" in resp.json()


def test_post_receipt_verify_stays_public():
    # The stateless POST verifier stays anonymous: no key ⇒ it reaches the route
    # (body validation may 422, but it is NEVER blocked by auth with 401).
    resp = _client().post("/v1/gcrumbs/verify", json={})
    assert resp.status_code != 401, f"POST verify must stay public, got 401: {resp.text}"
