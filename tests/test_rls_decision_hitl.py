"""Track 1 — RLS enforcement PROOF for decision_records + HITL (+ memories).

Self-provisions a restricted role (NOSUPERUSER NOBYPASSRLS, non-owner) — the real Phase-C
condition — applies the migration's ENABLE + FORCE + tenant_isolation policy, and proves the
DATABASE enforces isolation under the actual autocommit/transaction semantics the app uses:

  * with context = tenant A, only A's rows are visible (and B's are not);
  * UNSET context ⇒ 0 rows (FAIL-CLOSED — a forgotten context leaks nothing);
  * WITH CHECK blocks inserting another tenant's row.

These FAIL (not skip) if enforcement is off — unlike test_cgr_rls.py which skips when the
DB role can bypass. We MANUFACTURE a non-bypassing role so the assertions always run.

Covers the 4 policied tables. The full-mechanism proof (positive + WITH CHECK) runs on a
dedicated table; each REAL table is then proven to fail-close (unset ⇒ 0 rows) under the
same policy, without coupling to per-table NOT-NULL schemas.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from aml.server.tenant_context import apply_tenant_context, current_tenant

OWNER_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"
RT_ROLE = "rls_proof_rt"
RT_PW = "rtpw"
RT_URL = f"postgresql://{RT_ROLE}:{RT_PW}@localhost:5432/grafomem"

REAL_TABLES = ["decision_records", "hitl_approval_requests", "hitl_approvers", "memories"]

_POLICY = """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE  ROW LEVEL SECURITY;
    DO $$ BEGIN
      CREATE POLICY {p} ON {t}
        USING      (tenant_id = current_setting('app.current_tenant', true))
        WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
    EXCEPTION WHEN duplicate_object THEN null; END $$;
"""


def _owner():
    return psycopg.connect(OWNER_URL, autocommit=True)


@pytest.fixture(scope="module")
def enforcing():
    """Yield (url, role_or_None) for a connection RLS actually enforces against.

    Preferred: a self-provisioned non-owner NOSUPERUSER NOBYPASSRLS role — the exact
    Phase-C condition (works where the test role can CREATE ROLE, e.g. CI's postgres-image
    superuser). Fallback: the OWNER itself, IFF it is non-super/non-bypass — then FORCE RLS
    subjects even the owner (strictly the harder case). SKIP only when the sole available
    role is superuser/bypassrls (RLS is inert → nothing to prove; must be run elsewhere)."""
    with _owner() as c:
        sup, byp, cr = c.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
    if sup or cr:
        with _owner() as c:
            c.execute(f"DROP ROLE IF EXISTS {RT_ROLE}")
            c.execute(f"CREATE ROLE {RT_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{RT_PW}'")
            c.execute(f"GRANT USAGE ON SCHEMA public TO {RT_ROLE}")
            c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RT_ROLE}")
            c.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RT_ROLE}")
        yield RT_URL, RT_ROLE
        with _owner() as c:
            c.execute(f"REASSIGN OWNED BY {RT_ROLE} TO grafomem")
            c.execute(f"DROP OWNED BY {RT_ROLE}")
            c.execute(f"DROP ROLE IF EXISTS {RT_ROLE}")
    elif not sup and not byp:
        yield OWNER_URL, None                         # owner + FORCE enforces (grafomem is non-super/non-bypass)
    else:
        pytest.skip("only a superuser/bypassrls role available — RLS inert; prove under a restricted role")


def _set_ctx(conn, tenant):
    """Mimic the app: set the ContextVar, then apply it on the connection."""
    tok = current_tenant.set(tenant)
    try:
        apply_tenant_context(conn)
    finally:
        current_tenant.reset(tok)


# ── full-mechanism proof on a dedicated table ────────────────────────────────

def test_mechanism_enforces_isolation_and_fail_closed(enforcing):
    url, role = enforcing
    tbl = f"rls_proof_{uuid.uuid4().hex[:8]}"
    A, B = f"tenA-{uuid.uuid4().hex[:6]}", f"tenB-{uuid.uuid4().hex[:6]}"
    with _owner() as c:
        c.execute(f"CREATE TABLE {tbl} (tenant_id text NOT NULL, val text)")
        if role:
            c.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {tbl} TO {role}")
        # seed BEFORE the policy — once FORCE + WITH CHECK is on, even the owner needs context
        c.execute(f"INSERT INTO {tbl} VALUES (%s,'a1'),(%s,'a2'),(%s,'b1')", (A, A, B))
        c.execute(_POLICY.format(t=tbl, p=f"iso_{tbl}"))
    try:
        # connect as the enforcing (non-bypassing) role — RLS is live for it
        with psycopg.connect(url) as rc:              # autocommit off (real app txn semantics)
            # (1) scoped to A ⇒ sees only A's 2 rows, never B
            _set_ctx(rc, A)
            rows = rc.execute(f"SELECT val FROM {tbl} ORDER BY val").fetchall()
            assert [r[0] for r in rows] == ["a1", "a2"], rows

            # (2) scoped to B ⇒ only B
            _set_ctx(rc, B)
            assert [r[0] for r in rc.execute(f"SELECT val FROM {tbl}").fetchall()] == ["b1"]

            # (3) FAIL-CLOSED: no context ⇒ '' ⇒ 0 rows (never leaks)
            _set_ctx(rc, None)
            assert rc.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0] == 0

            # (4) WITH CHECK: scoped to A cannot INSERT a B-tenant row
            _set_ctx(rc, A)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                rc.execute(f"INSERT INTO {tbl} VALUES (%s,'evil')", (B,))
            rc.rollback()
    finally:
        with _owner() as c:
            c.execute(f"DROP TABLE IF EXISTS {tbl}")


# ── each REAL table fails closed under the same policy ───────────────────────

@pytest.mark.parametrize("table", REAL_TABLES)
def test_real_table_fails_closed_under_rls(enforcing, table):
    """Apply ENABLE+FORCE+policy to the real table; the enforcing role with UNSET context
    must see 0 rows (fail-closed). Proves the policy binds this table's tenant_id correctly,
    without seeding its NOT-NULL schema. Teardown restores the table's prior RLS state."""
    url, _role = enforcing
    pol = f"iso_proof_{table}"
    with _owner() as c:
        had_rls = c.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=%s", (table,)
        ).fetchone()
        c.execute(_POLICY.format(t=table, p=pol))
    try:
        with psycopg.connect(url) as rc:
            _set_ctx(rc, None)                       # unset ⇒ '' ⇒ fail-closed
            assert rc.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, \
                f"{table}: unset context must yield 0 rows (fail-closed)"
            _set_ctx(rc, f"nonexistent-{uuid.uuid4().hex}")  # unknown tenant ⇒ 0 rows too
            assert rc.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        with _owner() as c:
            c.execute(f"DROP POLICY IF EXISTS {pol} ON {table}")
            # restore prior FORCE/ENABLE (memories was already ENABLED pre-test)
            if had_rls and not had_rls[1]:
                c.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            if had_rls and not had_rls[0]:
                c.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
