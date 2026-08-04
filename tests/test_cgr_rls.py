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
