"""CGR issuer auth-exemption boundary (#4a follow-up).

`GET /v1/cgr/issuer` publishes the Foundation public key a verifier (GEIANT)
fetches + pins, so it must bypass tenant auth — mirroring /v1/gcrumbs/verify/key.
The exemption is EXACT-MATCH only: the /v1/cgr/attestation(s) endpoints, which
read tenant substrate, must still require a key. These two tests lock that line.
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


def test_issuer_is_auth_exempt():
    # NO auth header. Exempt ⇒ the request reaches the route, so it returns 200
    # (Foundation seed set) or 503 (seed unset) — but NEVER 401. If this 401s, the
    # exemption regressed; if it 404s, the CGR issuance router failed to mount.
    resp = _client().get("/v1/cgr/issuer")
    assert resp.status_code in (200, 503), f"expected 200/503, got {resp.status_code}: {resp.text}"
    assert resp.status_code != 401


def test_attestations_still_require_auth():
    # The attestation endpoints read tenant substrate — exact-match exemption must
    # NOT leak to them. No key ⇒ 401 from the auth middleware.
    resp = _client().get("/v1/cgr/attestations")
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"
