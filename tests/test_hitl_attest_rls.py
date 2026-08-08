"""Regression: HITL self-authenticated attest / fetch / inbox under FORCE RLS as a restricted role.

This is the test whose absence let the bug ship. It drives the REAL self-authenticated endpoints
(Ed25519 signature; the `/v1/hitl/requests/{id}*` + `/v1/hitl/approvers/*` auth-skip → the middleware
pins `default_namespace`) against Postgres as a NOSUPERUSER NOBYPASSRLS role with FORCE RLS on
`hitl_approval_requests` + `hitl_approvers` — the exact prod condition that 404'd the graduation gate.

Unlike the other HITL tests it uses NO mock db_pool (which never exercises RLS) and NO token-auth
(which would sidestep the default_namespace path). The Option-A fix's SECURITY DEFINER resolvers let
each handler scope to the request's owning tenant, so the lookups succeed under the correct RLS.

Pre-fix, the raw `WHERE request_id` lookup under `default_namespace` returns 0 rows (asserted
directly). Post-fix, the endpoints resolve→scope→succeed.

Requires a superuser role (to own the RLS-bypassing SD functions + provision the restricted role);
skips otherwise (a local non-superuser dev role). CI's POSTGRES_USER=grafomem is a superuser.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aml.cloud.hitl_routes as hitl_routes
from aml.cloud.db_pool import DatabasePool
from aml.cloud.hitl_routes import create_hitl_router
from aml.server.auth import TenantAuthMiddleware
from aml.server.tenant_context import apply_tenant_context, current_tenant

OWNER_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"
RT_ROLE = "hitl_rls_rt"
RT_PW = "rtpw"
RT_URL = f"postgresql://{RT_ROLE}:{RT_PW}@localhost:5432/grafomem"

_HITL_TABLES = ["hitl_approval_requests", "hitl_approvers"]

_POLICY = """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE  ROW LEVEL SECURITY;
    DO $$ BEGIN
      CREATE POLICY {p} ON {t}
        USING      (tenant_id = current_setting('app.current_tenant', true))
        WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
    EXCEPTION WHEN duplicate_object THEN null; END $$;
"""

# The fix's resolvers — created here owned by the (superuser) test role so SECURITY DEFINER bypasses
# FORCE RLS, mirroring the prod superuser deploy of ops/hitl_tenant_resolvers.sql.
_RESOLVERS = """
CREATE OR REPLACE FUNCTION public.hitl_request_tenant(p_request_id text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
AS $$ SELECT tenant_id FROM public.hitl_approval_requests WHERE request_id = p_request_id; $$;
CREATE OR REPLACE FUNCTION public.hitl_approver_tenants(p_approver_id text)
RETURNS text[] LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
AS $$ SELECT array_agg(tenant_id) FROM public.hitl_approvers WHERE approver_id = p_approver_id AND active = TRUE; $$;
"""


def _owner():
    return psycopg.connect(OWNER_URL, autocommit=True)


@pytest.fixture()
def rls_env(monkeypatch):
    """Superuser-owned SD resolvers + a restricted NOSUPERUSER NOBYPASSRLS role + FORCE RLS on the
    HITL tables. Yields the restricted DSN. Skips unless the current role is a superuser."""
    with _owner() as c:
        sup = c.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
    if not sup:
        pytest.skip("needs a superuser to own the RLS-bypassing SD resolvers + provision a "
                    "restricted role (runs in CI; POSTGRES_USER=grafomem is superuser)")

    prior = {}
    with _owner() as c:
        # SD resolvers owned by this superuser → SECURITY DEFINER bypasses FORCE RLS
        c.execute(_RESOLVERS)
        # restricted runtime role (the real Phase-C condition)
        c.execute(f"DROP ROLE IF EXISTS {RT_ROLE}")
        c.execute(f"CREATE ROLE {RT_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{RT_PW}'")
        c.execute(f"GRANT USAGE ON SCHEMA public TO {RT_ROLE}")
        c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RT_ROLE}")
        c.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RT_ROLE}")
        c.execute("REVOKE EXECUTE ON FUNCTION public.hitl_request_tenant(text) FROM PUBLIC")
        c.execute("REVOKE EXECUTE ON FUNCTION public.hitl_approver_tenants(text) FROM PUBLIC")
        c.execute(f"GRANT EXECUTE ON FUNCTION public.hitl_request_tenant(text) TO {RT_ROLE}")
        c.execute(f"GRANT EXECUTE ON FUNCTION public.hitl_approver_tenants(text) TO {RT_ROLE}")
        for t in _HITL_TABLES:
            prior[t] = c.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=%s", (t,)
            ).fetchone()
            c.execute(_POLICY.format(t=t, p=f"iso_hitltest_{t}"))

    # prod has UNSAFE_LOCAL_DEV off; conftest sets it on — force the prod path so the 401 negative
    # (unregistered/forged signer is rejected, never auto-registered) is faithful.
    monkeypatch.setattr(hitl_routes, "_unsafe_dev_enabled", lambda: False)

    yield RT_URL

    with _owner() as c:
        for t in _HITL_TABLES:
            c.execute(f"DROP POLICY IF EXISTS iso_hitltest_{t} ON {t}")
            had = prior.get(t)
            if had and not had[1]:
                c.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            if had and not had[0]:
                c.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
        c.execute("DROP FUNCTION IF EXISTS public.hitl_request_tenant(text)")
        c.execute("DROP FUNCTION IF EXISTS public.hitl_approver_tenants(text)")
        c.execute(f"REASSIGN OWNED BY {RT_ROLE} TO grafomem")
        c.execute(f"DROP OWNED BY {RT_ROLE}")
        c.execute(f"DROP ROLE IF EXISTS {RT_ROLE}")


class _Orch:
    def __init__(self):
        self.executed = []

    def execute_approved_action(self, tenant_id, workflow_id, proposed_action):
        self.executed.append((tenant_id, workflow_id, proposed_action))
        return {"executed": True}

    def resume_workflow(self, workflow_id, approved):
        pass

    def get_workflow(self, workflow_id):
        return None if str(workflow_id).startswith("propose:") else object()


class _Gcrumbs:
    def append_breadcrumb(self, *a, **k):
        pass


def _key():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return priv, pub


def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _seed(tenant, request_id, approver_pub, proposed_action):
    """Seed a pending request + an active approver for `tenant`, as the superuser owner (which
    bypasses RLS). Returns the exact context_bytes the approver must sign."""
    ctx = {"request_id": request_id, "tenant_id": tenant, "workflow_id": f"propose:{request_id}",
           "step_id": "s1", "action": "deploy", "resource": "prod", "proposed_action": proposed_action}
    context_bytes = json.dumps(ctx).encode("utf-8")
    now = datetime.now(timezone.utc)
    with _owner() as c:
        c.execute(
            "INSERT INTO hitl_approval_requests (request_id, tenant_id, workflow_id, step_id, action, "
            "resource, context_json, context_bytes, nonce, issued_at, expires_at, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')",
            (request_id, tenant, ctx["workflow_id"], "s1", "deploy", "prod",
             json.dumps(ctx), context_bytes, uuid.uuid4().hex, now, now + timedelta(hours=1)),
        )
        c.execute(
            "INSERT INTO hitl_approvers (approver_id, tenant_id, public_key, active) VALUES (%s,%s,%s,TRUE)",
            (approver_pub, tenant, approver_pub),
        )
    return context_bytes


def _cleanup(tenant, request_id, approver_pub):
    with _owner() as c:
        c.execute("DELETE FROM hitl_approval_requests WHERE request_id=%s", (request_id,))
        c.execute("DELETE FROM hitl_approvers WHERE approver_id=%s", (approver_pub,))


def test_self_authenticated_hitl_path_works_under_force_rls(rls_env):
    A = f"hitlA-{uuid.uuid4().hex[:8]}"
    rid = f"req-{uuid.uuid4().hex[:12]}"
    approver_priv, approver_pub = _key()
    proposed = {"tool": "deploy", "args": {"target": "prod"}, "invoice_ref": "PR-GRAD-1"}
    context_bytes = _seed(A, rid, approver_pub, proposed)

    try:
        # ── PRE-FIX evidence: the raw WHERE-request_id lookup as the restricted role under the
        #    default_namespace context returns 0 rows (exactly why attest 404'd pre-fix) ──
        with psycopg.connect(RT_URL) as rc:
            tok = current_tenant.set("default_namespace")
            try:
                apply_tenant_context(rc)
            finally:
                current_tenant.reset(tok)
            raw = rc.execute("SELECT count(*) FROM hitl_approval_requests WHERE request_id=%s", (rid,)).fetchone()[0]
            assert raw == 0, "pre-fix: raw lookup under default_namespace must fail-closed (0 rows)"
            # the SD resolver (bypasses RLS) still returns the owning tenant — and ONLY the id
            resolved = rc.execute("SELECT public.hitl_request_tenant(%s)", (rid,)).fetchone()[0]
            assert resolved == A

        # ── the REAL self-authenticated endpoints, restricted role + FORCE RLS ──
        pool = DatabasePool(RT_URL)
        pool.open()
        orch = _Orch()
        app = FastAPI()
        app.add_middleware(TenantAuthMiddleware, auth_mode="none")  # hitl paths auth-skip → default_namespace
        app.include_router(create_hitl_router(pool, orch, _Gcrumbs()))
        client = TestClient(app)
        try:
            # (1) fetch: signed challenge → 200 with the context bytes (pre-fix: 404)
            ts = str(_now_ms())
            fetch_sig = approver_priv.sign(f"grafomem.hitl.fetch.v1:{rid}:{ts}".encode()).hex()
            r = client.get(f"/v1/hitl/requests/{rid}",
                           headers={"X-GNS-Signature": fetch_sig, "X-GNS-Timestamp": ts,
                                    "X-GNS-Signer": approver_pub})
            assert r.status_code == 200, r.text
            assert r.json()["context_bytes_hex"] == context_bytes.hex()

            # (2) inbox: signed challenge → 200, our pending request present (pre-fix: 403 empty)
            ts = str(_now_ms())
            inbox_sig = approver_priv.sign(f"grafomem.hitl.inbox.v1:{approver_pub}:{ts}".encode()).hex()
            r = client.get(f"/v1/hitl/approvers/{approver_pub}/requests",
                           headers={"X-GNS-Signature": inbox_sig, "X-GNS-Timestamp": ts})
            assert r.status_code == 200, r.text
            assert rid in [x["request_id"] for x in r.json()["requests"]]

            # (3) self-approve NEGATIVE: registered approver's signer_id, but a FORGED signature
            #     from a key the caller controls → 401 (row IS found post-fix; signature fails)
            forger, _ = _key()
            bad = forger.sign(b"grafomem.hitl.approval.v1:" + context_bytes + b"\x1f" + b"approve").hex()
            r = client.post(f"/v1/hitl/requests/{rid}/attest",
                            json={"decision": "approve", "signer_id": approver_pub, "signature": bad})
            assert r.status_code == 401, r.text
            assert orch.executed == []

            # (4) genuine approval by the registered approver → 200, the committed action executes
            good = approver_priv.sign(b"grafomem.hitl.approval.v1:" + context_bytes + b"\x1f" + b"approve").hex()
            r = client.post(f"/v1/hitl/requests/{rid}/attest",
                            json={"decision": "approve", "signer_id": approver_pub, "signature": good})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "approved"
            assert orch.executed == [(A, f"propose:{rid}", proposed)]
        finally:
            pool.close()
    finally:
        _cleanup(A, rid, approver_pub)
