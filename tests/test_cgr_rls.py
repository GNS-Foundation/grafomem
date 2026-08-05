"""CGR #12 — RLS-enforcement code half (grafomem).

Two concerns:
  1. ensure_schema gating — the backend can init WITHOUT attempting startup DDL
     (so it's safe to boot under the restricted runtime role grafomem_rt).
  2. scoped_audit — tenant reads route through the REAL tenant RLS context, so the
     DATABASE enforces isolation. Whether the DB *actually* enforces depends on the
     connecting role: the isolation/fail-closed assertions run ONLY when the test
     DB role cannot bypass RLS; otherwise they SKIP with an explicit message (they
     must be verified on staging under grafomem_rt — never silently pass).
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from aml.backends.interface import WriteOptions
from aml.backends.postgres_gmp import PostgresGMPBackend
from aml.cgr.substrate import (
    CGR_OUTCOME_SCHEMA, _scoped_audit, _tenant_outcomes,
)
from aml.server.stores import StoreManager

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def _tenant() -> str:
    return f"rls-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def backend():
    """A cgr-outcomes store backend, CLOSED after the test so its pool doesn't
    linger (a lingering pool can hold connections past the per-test TRUNCATE)."""
    store = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL))
    b = store.get_or_create_named("cgr-outcomes").backend
    try:
        yield b
    finally:
        b.close()


def _write_outcome(backend, tenant_id: str, inv: str, outcome: str = "paid") -> None:
    meta = {"cgr_schema": CGR_OUTCOME_SCHEMA, "predicate": "receivable_outcome",
            "subject": inv, "object": outcome}
    backend.write(f"receivable_outcome | {inv} | {outcome}", WriteOptions(tenant_id=tenant_id, metadata=meta))


def _rls_enforceable() -> tuple[bool, str]:
    """Can the test DB role actually have RLS enforced against it? Only if it is
    NOT superuser, NOT BYPASSRLS, and either a non-owner of `memories` OR the table
    has FORCE ROW LEVEL SECURITY."""
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        rolsuper, rolbypass = cur.fetchone()
        if rolsuper:
            return False, "current_user is a superuser (RLS inert)"
        if rolbypass:
            return False, "current_user has BYPASSRLS"
        cur.execute("SELECT relforcerowsecurity, pg_get_userbyid(relowner) = current_user "
                    "FROM pg_class WHERE relname = 'memories'")
        row = cur.fetchone()
        if row is None:
            return False, "memories table not found"
        force, is_owner = row
        if is_owner and not force:
            return False, "current_user OWNS memories without FORCE ROW LEVEL SECURITY"
        return True, "ok"


# ---------------------------------------------------------------------------
# 1. ensure_schema gating (no RLS role needed — pure init-path logic)
# ---------------------------------------------------------------------------

def test_ensure_schema_gating(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(PostgresGMPBackend, "_ensure_schema", lambda self: calls.append(1))

    b_off = PostgresGMPBackend(TEST_DB_URL, ensure_schema=False)
    try:
        assert calls == []                       # NO startup DDL attempted (safe under restricted role)
    finally:
        b_off.close()

    b_on = PostgresGMPBackend(TEST_DB_URL, ensure_schema=True)
    try:
        assert calls == [1]                      # migrator path ran
    finally:
        b_on.close()

    calls.clear()
    monkeypatch.setenv("GRAFOMEM_DB_ENSURE_SCHEMA", "false")   # env default drives it
    b_env = PostgresGMPBackend(TEST_DB_URL)
    try:
        assert calls == []
    finally:
        b_env.close()


# ---------------------------------------------------------------------------
# 2. scoped_audit — regression equivalence (runs under ANY role, incl. superuser)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scoped_audit_regression_equivalence(backend):
    """scoped_audit(T) restricted to T == audit() filtered to T — identical results
    whether RLS enforces (restricted role) or not (superuser + Python filter). This
    is the backward-compat guarantee: load_reviews/load_rotations/compute_scores are
    unchanged for a valid tenant."""
    T = _tenant()
    for i in range(3):
        _write_outcome(backend, T, f"{T}-INV{i}")

    scoped_refs = {m.ref for m in _scoped_audit(backend, T) if m.tenant_id == T}
    admin_refs = {m.ref for m in backend.audit() if m.tenant_id == T}
    assert scoped_refs == admin_refs and len(scoped_refs) == 3
    # the substrate helper returns exactly this tenant's outcomes
    assert len(_tenant_outcomes(backend, T)) == 3


# ---------------------------------------------------------------------------
# 2a. scoped_audit under a BYPASS role — reproduces the prod 500 (#12a)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scoped_audit_filters_in_sql_under_bypass_role(backend, monkeypatch):
    """Reproduce the prod /v1/cgr/scores 500 (#12a) and prove the fix.

    On a connection where RLS does NOT enforce (the default owner/superuser test
    role — the exact prod situation), scoped_audit(A) must return ONLY tenant A's
    rows AND must never MATERIALIZE tenant B's row. Row materialization runs
    `_row_to_memory`, which DECRYPTS content — so before the SQL `WHERE tenant_id`
    fix, scoped_audit returned every tenant's rows and decrypting a foreign/legacy
    row raised "Decryption failures are strictly denied" → HTTP 500. The caller's
    Python `tenant_id ==` filter can't help: it runs AFTER decryption.

    This exercises the bypass-role read path the #12 self-provisioned-role test
    (which proves the restricted role) does not. In CI current_user is a superuser,
    so this RUNS; it SKIPS only on an RLS-enforcing role (there the DB filters anyway)."""
    enforceable, why = _rls_enforceable()
    if enforceable:
        pytest.skip(f"needs a NON-enforcing (bypass) role to reproduce the prod path ({why}); "
                    f"runs under the CI superuser. RLS-enforcing roles filter at the DB regardless.")

    A, B = _tenant(), _tenant()
    _write_outcome(backend, A, f"{A}-INV0")
    _write_outcome(backend, A, f"{A}-INV1")
    _write_outcome(backend, B, f"{B}-SECRET")

    # Spy on materialization: record the tenant_id column (index 6) of every raw row
    # that reaches _row_to_memory, to PROVE B's row is never decrypted.
    materialized: list[str] = []
    orig_row_to_memory = backend._row_to_memory
    def _spy(row, *args, **kwargs):
        materialized.append(row[6])
        return orig_row_to_memory(row, *args, **kwargs)
    monkeypatch.setattr(backend, "_row_to_memory", _spy)

    rows = list(backend.scoped_audit(A))

    assert {m.tenant_id for m in rows} == {A}          # only A's rows returned (no B leak)
    assert len(rows) == 2
    assert B not in materialized                        # B's row NEVER materialized/decrypted
    assert materialized and all(t == A for t in materialized)


# ---------------------------------------------------------------------------
# 3. DB-level isolation + fail-closed — RLS-enforceable roles only (else SKIP)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scoped_audit_db_level_isolation(backend):
    enforceable, why = _rls_enforceable()
    if not enforceable:
        pytest.skip(f"RLS not enforceable for this DB role ({why}) — DB-level isolation "
                    f"MUST be verified on staging under grafomem_rt (NOSUPERUSER/NOBYPASSRLS, "
                    f"non-owner). Not silently passing.")

    A, B = _tenant(), _tenant()
    _write_outcome(backend, A, f"{A}-SECRET")
    _write_outcome(backend, B, f"{B}-OWN")

    # scoped_audit has NO Python tenant filter — isolation here is the DB's (RLS).
    b_rows = list(backend.scoped_audit(B))
    assert b_rows, "tenant B should see its own row"
    assert all(m.tenant_id == B for m in b_rows)              # zero of A's rows leak
    subjects = {(m.metadata or {}).get("subject") for m in b_rows}
    assert f"{A}-SECRET" not in subjects


def _can_create_role() -> bool:
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user")
        return bool(cur.fetchone()[0])


@pytest.mark.asyncio
async def test_rls_proven_under_self_provisioned_restricted_role(backend):
    """PROVE RLS at the DB level by SELF-PROVISIONING a restricted role (mirrors the
    future grafomem_rt: NOSUPERUSER/NOBYPASSRLS, non-owner) — so this RUNS in CI
    (superuser) instead of skipping. Skips ONLY when current_user cannot CREATE ROLE.

    Seeds tenants A and B as the normal (privileged) backend, then reads through the
    shipped scoped_audit path AS the restricted role: RLS must show only B's rows and
    zero of A's, with NO Python tenant filter; unset tenant ⇒ zero rows (fail-closed)."""
    from psycopg import sql

    if not _can_create_role():
        pytest.skip("current_user lacks CREATEROLE — cannot self-provision a restricted role "
                    "to prove RLS locally. In CI current_user is a superuser and this RUNS.")

    role = "rls_rt_" + uuid.uuid4().hex[:12]
    pw = uuid.uuid4().hex
    restricted_url = f"postgresql://{role}:{pw}@localhost:5432/grafomem"

    A, B = _tenant(), _tenant()
    _write_outcome(backend, A, f"{A}-SECRET")
    _write_outcome(backend, B, f"{B}-OWN")

    admin = psycopg.connect(TEST_DB_URL, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS")
                        .format(sql.Identifier(role), sql.Literal(pw)))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON memories, memory_embeddings TO {}")
                        .format(sql.Identifier(role)))
            cur.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}")
                        .format(sql.Identifier(role)))

        # (a) Independent, backend-free proof: a raw SELECT AS the restricted role,
        # under SET app.current_tenant — pure DB enforcement, no grafomem code at all.
        with psycopg.connect(restricted_url, autocommit=True) as rc, rc.cursor() as rcur:
            rcur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            rs, rb = rcur.fetchone()
            assert rs is False and rb is False, "restricted role must be NOSUPERUSER/NOBYPASSRLS"
            rcur.execute("SELECT set_config('app.current_tenant', %s, false)", (B,))
            rcur.execute("SELECT DISTINCT tenant_id FROM memories")
            tids = {r[0] for r in rcur.fetchall()}
            assert tids == {B}, f"RLS must restrict the restricted role to tenant B, saw {tids}"
            rcur.execute("SELECT set_config('app.current_tenant', '', false)")   # fail-closed
            rcur.execute("SELECT count(*) FROM memories")
            assert rcur.fetchone()[0] == 0, "unset tenant must yield zero rows (fail-closed)"

        # (b) Same enforcement via the SHIPPED scoped_audit path (_tenant_conn) — NO Python filter
        rt = PostgresGMPBackend(restricted_url, ensure_schema=False)   # no DDL under the restricted role
        try:
            b_rows = list(rt.scoped_audit(B))
            assert b_rows, "tenant B must see its own rows"
            assert all(m.tenant_id == B for m in b_rows)                # zero of A leaks
            assert f"{A}-SECRET" not in {(m.metadata or {}).get("subject") for m in b_rows}
            # cross-check: A's scope also excludes B
            assert all(m.tenant_id == A for m in rt.scoped_audit(A))
            # fail-closed: empty tenant context ⇒ zero rows
            assert list(rt.scoped_audit("")) == []
        finally:
            rt.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                "WHERE usename = {}").format(sql.Literal(role)))
            cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


@pytest.mark.asyncio
async def test_scoped_audit_fail_closed_unset_tenant(backend):
    enforceable, why = _rls_enforceable()
    if not enforceable:
        pytest.skip(f"RLS not enforceable for this DB role ({why}) — fail-closed MUST be "
                    f"verified on staging under grafomem_rt. Not silently passing.")

    T = _tenant()
    _write_outcome(backend, T, f"{T}-INV")

    # empty tenant context ⇒ RLS matches only tenant_id='' ⇒ zero rows (fail-closed)
    assert list(backend.scoped_audit("")) == []
