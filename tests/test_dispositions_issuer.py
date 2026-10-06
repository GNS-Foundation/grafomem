"""GB3: GET /v1/dispositions/issuer — the runtime issuer key, fetchable without a key, never trusted on
first fetch (GD2: the key and fingerprint travel in the onboarding pack and are confirmed out of band).

Response: issuer, issuer_key_id ("ed25519:<64 hex>"), public_key (64 hex), algorithm "ed25519",
key_id_grouped (the 64 hex digits in groups of 4 — the read-aloud form), trust_note, key_history null.
Unauthenticated via an EXACT entry in the auth skip list (so /v1/dispositions/issuerx stays 401);
503 when the runtime has no signing identity; Cache-Control public, max-age=300, must-revalidate;
ETag = the key id.

MUST-FAIL on main 5f2896b: the route does not exist — an unauthenticated GET is 401 (auth answers
before routing) and an authenticated GET is 404.
"""
import json
import os
import pathlib
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aml.cloud.tenant_manager import TenantManager
from aml.server.app import create_app

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
SIGNING_SEED = "b" * 64
PUB_HEX = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(SIGNING_SEED)).public_key().public_bytes_raw().hex()
PATH = "/v1/dispositions/issuer"


@pytest.fixture(scope="module")
def app_instance():
    os.environ["GRAFOMEM_DB_URL"] = DB_URL
    os.environ["GRAFOMEM_AUTH_MODE"] = "cloud"
    os.environ["GRAFOMEM_SIGNING_KEY"] = SIGNING_SEED
    from tests.test_disposition_route import _apply_migration_016
    _apply_migration_016()
    return create_app(db_url=DB_URL)


@pytest.fixture(scope="module")
def client(app_instance):
    with TestClient(app_instance) as c:
        assert c.get("/health").status_code == 200
        yield c


@pytest.fixture
def admin_key():
    tm = TenantManager(DB_URL); tm.ensure_schema()
    t = tm.create_tenant(name=f"gb3-{uuid.uuid4().hex[:8]}")
    from aml.server.scopes import TENANT_ADMIN_SCOPES
    return tm.create_api_key(t.id, name="gb3", role="admin", scopes=TENANT_ADMIN_SCOPES)["api_key"]


# ---------------------------------------------------------------- 1. unauthenticated, exact shape

def test_issuer_is_public_and_matches_the_signing_seed(client):
    r = client.get(PATH)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    d = r.json()
    assert d["issuer"] == "grafomem-runtime"
    assert d["issuer_key_id"] == "ed25519:" + PUB_HEX
    assert d["public_key"] == PUB_HEX
    assert d["algorithm"] == "ed25519"
    assert d["key_id_grouped"] == " ".join(PUB_HEX[i:i + 4] for i in range(0, 64, 4))
    assert len(d["key_id_grouped"].split(" ")) == 16
    assert "trust" in d["trust_note"].lower() and "first fetch" in d["trust_note"].lower()
    assert d["key_history"] is None
    assert set(d) == {"issuer", "issuer_key_id", "public_key", "algorithm", "key_id_grouped", "trust_note", "key_history"}


# ---------------------------------------------------------------- 2. authenticated GET also 200

def test_issuer_with_a_key_is_200_not_404(client, admin_key):
    r = client.get(PATH, headers={"X-API-Key": admin_key})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert r.json()["issuer_key_id"] == "ed25519:" + PUB_HEX


# ---------------------------------------------------------------- 3. non-vacuity: a real record verifies under the route's key

def test_record_signed_through_the_real_flow_verifies_under_the_routes_public_key(client):
    from tests.test_disposition_route import _envelope, _hdr
    import psycopg
    from aml.cgr import cosign_verify as cv
    tm = TenantManager(DB_URL); tm.ensure_schema()
    t = tm.create_tenant(name=f"gb3-nv-{uuid.uuid4().hex[:8]}")
    disp_key = tm.create_api_key(t.id, name="disp", role="agent", scopes=["disposition:write", "disposition:read"])["api_key"]
    approver_sk = Ed25519PrivateKey.generate()
    approver_hex = approver_sk.public_key().public_bytes_raw().hex()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("INSERT INTO hitl_approvers (approver_id, tenant_id, public_key, active) VALUES (%s,%s,%s,TRUE)",
                  (f"did:person:{uuid.uuid4().hex[:8]}", t.id, approver_hex))
    r = client.post("/v1/dispositions", json=_envelope(approver_sk, approver_hex), headers=_hdr(disp_key))
    assert r.status_code == 201, r.text
    rid = r.json()["record_id"]
    issuer = client.get(PATH).json()
    assert r.json()["issuer_key_id"] == issuer["issuer_key_id"], "the route must publish the key that signed the record"
    g = client.get(f"/v1/dispositions/{rid}", headers=_hdr(disp_key))
    assert g.status_code == 200, g.text
    record = g.json()["record"]
    assert record["system_metadata"]["issuer_key_id"] == issuer["issuer_key_id"]
    registry = json.loads((_ROOT / "docs/cgr/cosign-profile-registry.json").read_text())
    res = cv.verify(record, registry, trusted_issuers={issuer["public_key"]})
    assert res.get("valid") is True, f"the record does not verify under the route's public_key: {res}"
    # and NOT under some other key — the trusted set is doing the work
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    assert cv.verify(record, registry, trusted_issuers={other}).get("valid") is not True


# ---------------------------------------------------------------- 4. exact match: no prefix bypass

def test_issuerx_is_not_public(client):
    r = client.get(PATH + "x")
    assert r.status_code == 401, f"{r.status_code} {r.text}"


# ---------------------------------------------------------------- 5. no signing identity → 503

def test_503_without_a_signing_identity():
    from aml.cloud.disposition_routes import create_disposition_router
    app = FastAPI()
    app.include_router(create_disposition_router(None, None))
    with TestClient(app) as c:
        r = c.get(PATH)
    assert r.status_code == 503, f"{r.status_code} {r.text}"


# ---------------------------------------------------------------- 6. caching headers

def test_cache_headers(client):
    r = client.get(PATH)
    assert r.headers.get("cache-control") == "public, max-age=300, must-revalidate"
    assert r.headers.get("etag") == f'"ed25519:{PUB_HEX}"'
