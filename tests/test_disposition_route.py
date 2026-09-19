"""End-to-end test for POST/GET /v1/dispositions (cgr.disposition.v1 runtime).

Exercises the full path against a local DB via TestClient: a real approver Ed25519 signature over the
§2.2 bytes, the runtime counter-signature, ledger persistence, and offline verification of the GET'd
record with the vendored reference verifier. Must-fail vectors + positive controls, per the corpus.
"""
import json
import os
import pathlib
import uuid

import psycopg
import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from aml.server.app import create_app
from aml.server.auth import TenantAuthMiddleware
from aml.cloud.tenant_manager import TenantManager
from aml.cgr import cosign_verify as cv

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
SIGNING_SEED = "b" * 64  # matches the app fixture; the runtime issuer key
_ISSUER_PUB = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(SIGNING_SEED)).public_key().public_bytes_raw().hex()


def _apply_migration_016():
    sql = (_ROOT / "src/aml/cloud/migrations/016_cosign_dispositions.sql").read_text()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("CREATE TABLE IF NOT EXISTS hitl_approvers (approver_id VARCHAR PRIMARY KEY, "
                  "tenant_id VARCHAR NOT NULL, public_key VARCHAR NOT NULL, active BOOLEAN DEFAULT TRUE)")
        c.execute(sql)


@pytest.fixture(scope="module")
def app_instance():
    os.environ["GRAFOMEM_DB_URL"] = DB_URL
    os.environ["GRAFOMEM_AUTH_MODE"] = "cloud"
    os.environ["GRAFOMEM_SIGNING_KEY"] = SIGNING_SEED
    _apply_migration_016()
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
def setup(app_instance):
    tm = TenantManager(DB_URL)
    tm.ensure_schema()
    info = tm.create_tenant(name=f"disp-{uuid.uuid4().hex[:8]}")
    disp_key = tm.create_api_key(info.id, name="disp", role="agent", scopes=["disposition:write", "disposition:read"])["api_key"]
    cgr_key = tm.create_api_key(info.id, name="cgr", role="agent", scopes=["cgr:read"])["api_key"]
    # Enrol an approver for this tenant.
    approver_sk = Ed25519PrivateKey.generate()
    approver_hex = approver_sk.public_key().public_bytes_raw().hex()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("INSERT INTO hitl_approvers (approver_id, tenant_id, public_key, active) VALUES (%s,%s,%s,TRUE)",
                  (f"did:person:{uuid.uuid4().hex[:8]}", info.id, approver_hex))
    return {"tenant_id": info.id, "disp_key": disp_key, "cgr_key": cgr_key,
            "approver_sk": approver_sk, "approver_hex": approver_hex}


def _envelope(approver_sk, approver_hex, *, nonce=None, act="approve"):
    body = {"kind": "disposition", "subject_ref": "hmac:pseudo-01", "decision": "approve"}
    a = {"content_digest": "b2-256:" + __import__("hashlib").blake2b(rfc8785.dumps(body), digest_size=32).hexdigest(),
         "approver_id": "did:person:test", "approver_key_id": "ed25519:" + approver_hex,
         "approver_act": act, "decision_date": "2026-09-19T00:00:00Z",
         "record_nonce": nonce or ("disp-" + uuid.uuid4().hex)}
    sig = "ed25519-sig:" + approver_sk.sign(cv.DOMAIN_TAG + rfc8785.dumps(a)).hex()
    return {"schema": "cgr.cosign.v1", "profile": "cgr.disposition.v1", "approval_mode": "bound",
            "content_body": body, "approval_assertion": a, "approver_signature": sig}


def _hdr(key): return {"X-API-Key": key}


def test_valid_disposition_201_and_offline_verifies(client, setup):
    env = _envelope(setup["approver_sk"], setup["approver_hex"])
    r = client.post("/v1/dispositions", json=env, headers=_hdr(setup["disp_key"]))
    assert r.status_code == 201, r.text
    rid = r.json()["record_id"]
    # GET returns the full envelope; it MUST verify offline under the pinned runtime issuer.
    g = client.get(f"/v1/dispositions/{rid}", headers=_hdr(setup["disp_key"]))
    assert g.status_code == 200, g.text
    assert g.json()["assurance"] == "none"  # surfaced (approver enrolled at default tier)
    registry = json.loads((_ROOT / "docs/cgr/cosign-profile-registry.json").read_text())
    res = cv.verify(g.json()["record"], registry, trusted_issuers={_ISSUER_PUB})
    assert res.get("valid") is True, f"offline verify failed: {res}"


def test_missing_approver_signature_422(client, setup):
    env = _envelope(setup["approver_sk"], setup["approver_hex"])
    del env["approver_signature"]
    r = client.post("/v1/dispositions", json=env, headers=_hdr(setup["disp_key"]))
    assert r.status_code == 422, r.text


def test_submitted_system_signature_422(client, setup):
    env = _envelope(setup["approver_sk"], setup["approver_hex"])
    env["system_signature"] = "ed25519-sig:deadbeef"
    r = client.post("/v1/dispositions", json=env, headers=_hdr(setup["disp_key"]))
    assert r.status_code == 422, r.text


def test_approver_not_enrolled_403(client, setup):
    stranger = Ed25519PrivateKey.generate()
    env = _envelope(stranger, stranger.public_key().public_bytes_raw().hex())
    r = client.post("/v1/dispositions", json=env, headers=_hdr(setup["disp_key"]))
    assert r.status_code == 403, r.text
    assert "approver_not_enrolled" in r.text


def test_scope_gate_cgr_read_denied(client, setup):
    env = _envelope(setup["approver_sk"], setup["approver_hex"])
    r = client.post("/v1/dispositions", json=env, headers=_hdr(setup["cgr_key"]))
    assert r.status_code == 403, r.text
    assert "disposition:write" in r.text


def test_nonce_replay_409(client, setup):
    nonce = "disp-replay-" + uuid.uuid4().hex
    env = _envelope(setup["approver_sk"], setup["approver_hex"], nonce=nonce)
    r1 = client.post("/v1/dispositions", json=env, headers=_hdr(setup["disp_key"]))
    assert r1.status_code == 201, r1.text
    env2 = _envelope(setup["approver_sk"], setup["approver_hex"], nonce=nonce)  # same nonce, re-signed
    r2 = client.post("/v1/dispositions", json=env2, headers=_hdr(setup["disp_key"]))
    assert r2.status_code == 409, r2.text
