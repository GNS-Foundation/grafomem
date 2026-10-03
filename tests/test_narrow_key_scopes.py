"""Narrow keys via explicit `scopes` on POST /v1/portal/api-keys (option (a), operator go 2026-10-03).

A tenant mints a key carrying ONLY what it names — e.g. disposition:write + disposition:read, the
analyst's submit key — instead of an admin key. The list is validated (vocabulary) and bounded by
TENANT_ADMIN_SCOPES; a request naming anything outside that set is refused WHOLE with 400 (never
trimmed). `scopes: []` is refused. `role` becomes optional: 'agent' when scopes are given, 'admin'
otherwise (today's behaviour).

MUST-FAIL FIRST: every test below fails on 94c665b, where the route has no `scopes` field — the field
is silently ignored and the mint falls back to the role default (admin → TENANT_ADMIN_SCOPES).
"""
import hashlib
import os
import pathlib
import time
import uuid

import psycopg
import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from aml.cgr import cosign_verify as cv
from aml.server.app import create_app
from aml.server.auth import TenantAuthMiddleware
from aml.server.scopes import TENANT_ADMIN_SCOPES

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
SIGNING_SEED = "b" * 64
NARROW = ["disposition:write", "disposition:read"]


def _apply_migration_016():
    sql = (_ROOT / "src/aml/cloud/migrations/016_cosign_dispositions.sql").read_text()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("CREATE TABLE IF NOT EXISTS hitl_approvers (approver_id VARCHAR PRIMARY KEY, "
                  "tenant_id VARCHAR NOT NULL, public_key VARCHAR NOT NULL, active BOOLEAN DEFAULT TRUE)")
        c.execute(sql)


@pytest.fixture(scope="module")
def app_instance():
    pytest.importorskip("bcrypt")
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


@pytest.fixture
def session(client):
    """A fresh tenant: (tenant_id, session token). The birth key is not used."""
    email = f"narrow-{uuid.uuid4().hex[:8]}@example.test"
    r = client.post("/v1/portal/signup", json={"email": email, "password": "correct-horse-battery", "name": "narrow"})
    assert r.status_code == 201, r.text
    return r.json()["tenant_id"], r.json()["token"]


def _mint(client, token, body):
    return client.post("/v1/portal/api-keys", json=body, headers={"Authorization": f"Bearer {token}"})


def _row(key_id):
    with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as c:
        return c.execute("SELECT role, scopes, expires_at FROM tenant_api_keys WHERE key_id=%s", (key_id,)).fetchone()


def _key_count(tenant_id):
    with psycopg.connect(DB_URL, autocommit=True) as c:
        return c.execute("SELECT count(*) FROM tenant_api_keys WHERE tenant_id=%s", (tenant_id,)).fetchone()[0]


def _enrol_approver(tenant_id):
    sk = Ed25519PrivateKey.generate()
    hexpub = sk.public_key().public_bytes_raw().hex()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("INSERT INTO hitl_approvers (approver_id, tenant_id, public_key, active) VALUES (%s,%s,%s,TRUE)",
                  (f"did:person:{uuid.uuid4().hex[:8]}", tenant_id, hexpub))
    return sk, hexpub


def _envelope(sk, hexpub):
    body = {"kind": "disposition", "subject_ref": "hmac:pseudo-01", "decision": "approve"}
    a = {"content_digest": "b2-256:" + hashlib.blake2b(rfc8785.dumps(body), digest_size=32).hexdigest(),
         "approver_id": "did:person:test", "approver_key_id": "ed25519:" + hexpub,
         "approver_act": "approve", "decision_date": "2026-10-03T00:00:00Z",
         "record_nonce": "disp-" + uuid.uuid4().hex}
    sig = "ed25519-sig:" + sk.sign(cv.DOMAIN_TAG + rfc8785.dumps(a)).hex()
    return {"schema": "cgr.cosign.v1", "profile": "cgr.disposition.v1", "approval_mode": "bound",
            "content_body": body, "approval_assertion": a, "approver_signature": sig}


# ── 1. a narrow mint is accepted and stored narrowly ────────────────────────────────────

def test_1_narrow_mint_stores_exactly_the_named_scopes(client, session):
    tid, token = session
    r = _mint(client, token, {"name": "submit", "scopes": NARROW})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["scopes"]) == sorted(NARROW), "response must echo the narrow scopes"
    row = _row(r.json()["key_id"])
    assert sorted(row["scopes"]) == sorted(NARROW), f"stored scopes must be exactly the pair, got {row['scopes']}"
    assert "keys:admin" not in row["scopes"] and "memory:write" not in row["scopes"]


# ── 2. the narrow key files a disposition: 201 ──────────────────────────────────────────

def test_2_narrow_key_submits_a_disposition_201(client, session):
    tid, token = session
    r = _mint(client, token, {"name": "submit", "scopes": NARROW})
    assert r.status_code == 200, r.text
    assert sorted(_row(r.json()["key_id"])["scopes"]) == sorted(NARROW)  # narrow, not admin (red on main)
    key = r.json()["api_key"]
    sk, hexpub = _enrol_approver(tid)
    p = client.post("/v1/dispositions", json=_envelope(sk, hexpub), headers={"X-API-Key": key})
    assert p.status_code == 201, p.text
    g = client.get(f"/v1/dispositions/{p.json()['record_id']}", headers={"X-API-Key": key})
    assert g.status_code == 200, g.text  # disposition:read is in the pair


# ── 3. the narrow key gets 403 on non-disposition admin routes ──────────────────────────

def test_3_narrow_key_403_on_admin_and_other_tenant_routes(client, session):
    tid, token = session
    r = _mint(client, token, {"name": "submit", "scopes": NARROW})
    assert r.status_code == 200, r.text
    key = r.json()["api_key"]
    # keys:admin — the destructive tenant route (own-tenant identity passes; the scope must not)
    rot = client.post(f"/v1/cloud/tenants/{tid}/rotate-key", headers={"X-API-Key": key})
    assert rot.status_code == 403, f"a submit-only key must not rotate the tenant's keys: {rot.status_code} {rot.text}"
    assert _key_count(tid) >= 2, "rotate-key must not have run (it deletes every key)"
    # artifacts:read — an ordinary tenant scope the pair does not carry
    art = client.get("/v1/artifacts/stats", headers={"X-API-Key": key})
    assert art.status_code == 403, f"{art.status_code} {art.text}"


# ── 4. refusals: superuser, platform gate, identity authority, unknown ──────────────────

@pytest.mark.parametrize("bad", [["*"], ["admin:platform"], ["calibration:write"], ["not:a:scope"]])
def test_4_refused_scopes_400(client, session, bad):
    tid, token = session
    before = _key_count(tid)
    r = _mint(client, token, {"name": "bad", "scopes": bad})
    assert r.status_code == 400, f"{bad}: {r.status_code} {r.text}"
    assert _key_count(tid) == before, "nothing may be minted on a refused request"


# ── 5. a mixed list is refused whole, never trimmed ─────────────────────────────────────

def test_5_mixed_list_refused_whole(client, session):
    tid, token = session
    before = _key_count(tid)
    r = _mint(client, token, {"name": "mixed", "scopes": ["disposition:write", "admin:platform"]})
    assert r.status_code == 400, f"{r.status_code} {r.text}"
    assert "admin:platform" in r.text, "the refusal must name the refused scope"
    assert _key_count(tid) == before, "refused whole: no key with the allowed subset may exist"


# ── 6. the resolver never widens: exactly the pair, through the cache too ───────────────

def test_6_resolver_never_widens(client, session, app_instance):
    tid, token = session
    r = _mint(client, token, {"name": "submit", "scopes": NARROW})
    assert r.status_code == 200, r.text
    key = r.json()["api_key"]
    node = getattr(app_instance, "middleware_stack", None)
    mw = None
    for _ in range(50):
        if isinstance(node, TenantAuthMiddleware):
            mw = node; break
        node = getattr(node, "app", None)
    assert mw is not None
    mw._cache_ttl = 60  # exercise the cache path
    try:
        first = mw._resolve_api_key(key)
        second = mw._resolve_api_key(key)  # from cache
        for res in (first, second):
            assert res is not None and sorted(res[2]) == sorted(NARROW), f"resolver widened the scopes: {res and res[2]}"
        # and the HTTP gate agrees after the cache is warm
        assert client.post(f"/v1/cloud/tenants/{tid}/rotate-key", headers={"X-API-Key": key}).status_code == 403
    finally:
        mw._api_key_cache.clear(); mw._cache_ttl = 0


# ── 7. the guide's §2.7 example shape works ─────────────────────────────────────────────

def test_7_guide_example_shape(client, session):
    tid, token = session
    exp = int(time.time()) + 4 * 3600
    r = _mint(client, token, {"name": "bank-submit-2026-10-03", "scopes": NARROW, "expires_at": exp})
    assert r.status_code == 200, r.text
    row = _row(r.json()["key_id"])
    assert row["role"] == "agent" and sorted(row["scopes"]) == sorted(NARROW)
    assert row["expires_at"] is not None and abs(row["expires_at"].timestamp() - exp) < 2
    assert r.json()["expires_at"] is not None


# ── 8. `scopes: []` is refused ──────────────────────────────────────────────────────────

def test_8_empty_scopes_refused(client, session):
    tid, token = session
    before = _key_count(tid)
    r = _mint(client, token, {"name": "empty", "scopes": []})
    assert r.status_code == 400, f"{r.status_code} {r.text}"
    assert _key_count(tid) == before


# ── 9. role resolution: agent when scopes are given; admin → TENANT_ADMIN_SCOPES otherwise ──

def test_9_role_defaults(client, session):
    tid, token = session
    narrow = _mint(client, token, {"name": "n", "scopes": NARROW})
    assert narrow.status_code == 200, narrow.text
    assert _row(narrow.json()["key_id"])["role"] == "agent", "omitting role with scopes must store role='agent'"
    plain = _mint(client, token, {"name": "p"})
    assert plain.status_code == 200, plain.text
    prow = _row(plain.json()["key_id"])
    assert prow["role"] == "admin" and sorted(prow["scopes"]) == sorted(TENANT_ADMIN_SCOPES), \
        "omitting both keeps today's behaviour (admin → TENANT_ADMIN_SCOPES)"
