"""CGR calibration — transaction-local tenant GUC + RLS row-isolation (task a2).

Companion to tests/test_world_model_tenant_guc.py. The calibration tables
(`agent_calibration`, `cgr_gate_config`) run RLS FORCE + WITH CHECK in prod. This
suite proves the CONVERTED write/read paths — `routes._write_calibration_audited` and
`engine._resolve_gate_for`, which now use the transaction-local `gate.calibration_tenant_tx`
helper instead of the old session-scoped `set_config(..., false)` + finally-reset — both:

  (a) work end-to-end and turn the B2b gate ON (happy path, any DB role), and
  (b) enforce REAL DB-level tenant isolation with no GUC leak across pooled-connection
      borrows (RLS-enforceable roles only; else SKIP with a staging message).

The isolation half exercises the real paths and asserts ACTUAL row-isolation (bare
SELECTs with no `WHERE tenant_id`, so only RLS can be doing the filtering), plus a
rolled-back cross-tenant WITH CHECK write — never silently passing under a bypass role.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


# ── an ed25519 mock signer for gcrumbs (mirrors the established test mock) ──
class _MockId:
    def __init__(self, k: bytes):
        self.k = k

    def sign(self, m: bytes):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        priv = Ed25519PrivateKey.from_private_bytes(self.k)
        return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return Ed25519PrivateKey.from_private_bytes(self.k).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)


_CREATE = (
    """CREATE TABLE IF NOT EXISTS agent_calibration (
         tenant_id text NOT NULL, agent_key text NOT NULL,
         calibration_weight double precision, n_observations integer NOT NULL DEFAULT 0,
         method text, as_of timestamptz NOT NULL DEFAULT now(),
         PRIMARY KEY (tenant_id, agent_key))""",
    """CREATE TABLE IF NOT EXISTS cgr_gate_config (
         tenant_id text PRIMARY KEY, tau double precision NOT NULL,
         cap_k double precision NOT NULL, enabled boolean NOT NULL DEFAULT true,
         updated_at timestamptz NOT NULL DEFAULT now())""",
)


def _apply_rls(cur, table: str) -> None:
    """Idempotently ENABLE + FORCE RLS + the tenant-isolation policy — matches the prod
    posture. FORCE makes RLS bite even the table owner, so this proves enforcement even
    when the test role owns the table (as long as it is NOSUPERUSER/NOBYPASSRLS)."""
    cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cur.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    cur.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = current_setting('app.current_tenant', true)) "
        "WITH CHECK (tenant_id = current_setting('app.current_tenant', true))")


def _rls_enforceable(table: str) -> tuple[bool, str]:
    """RLS actually enforced against the test role? Only if NOT superuser, NOT
    BYPASSRLS, and (non-owner OR the table has FORCE ROW LEVEL SECURITY)."""
    import psycopg
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        rolsuper, rolbypass = cur.fetchone()
        if rolsuper:
            return False, "current_user is a superuser (RLS inert)"
        if rolbypass:
            return False, "current_user has BYPASSRLS"
        cur.execute("SELECT relforcerowsecurity, pg_get_userbyid(relowner) = current_user "
                    "FROM pg_class WHERE relname = %s", (table,))
        row = cur.fetchone()
        if row is None:
            return False, f"{table} not found"
        force, is_owner = row
        if is_owner and not force:
            return False, f"current_user OWNS {table} without FORCE ROW LEVEL SECURITY"
        return True, "ok"


@pytest.fixture
def env():
    """Provision the calibration schema + RLS, a max_size=1 pool (so the SAME physical
    connection is reused across borrows — a leaked GUC would be observable), and a real
    gcrumbs on that pool. Tracks created tenants and deletes their rows on teardown."""
    try:
        import psycopg  # noqa: F401
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except Exception:
        pytest.skip("psycopg / psycopg_pool not installed")

    import psycopg
    try:
        admin = psycopg.connect(TEST_DB_URL, autocommit=True, connect_timeout=4)
    except Exception as e:
        pytest.skip(f"test DB unavailable: {e}")
    try:
        with admin.cursor() as cur:
            for ddl in _CREATE:
                cur.execute(ddl)
            _apply_rls(cur, "agent_calibration")
            _apply_rls(cur, "cgr_gate_config")
    finally:
        admin.close()

    try:
        pool = ConnectionPool(TEST_DB_URL, min_size=1, max_size=1,
                              kwargs={"row_factory": dict_row}, open=True)
    except Exception as e:
        pytest.skip(f"cannot open pool: {e}")

    from aml.cloud.gcrumbs import GcrumbsService
    gcrumbs = GcrumbsService(TEST_DB_URL, signing_identity=_MockId(uuid.uuid4().bytes * 2), pool=pool)
    gcrumbs.ensure_schema()

    tenants: list[str] = []
    yield SimpleNamespace(pool=pool, gcrumbs=gcrumbs, tenants=tenants)

    # teardown: delete each test tenant's rows under its own GUC (FORCE RLS ⇒ no blanket delete)
    from aml.cgr.gate import calibration_tenant_tx
    for t in tenants:
        try:
            with calibration_tenant_tx(pool, t) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM agent_calibration WHERE tenant_id = %s", (t,))
                cur.execute("DELETE FROM cgr_gate_config WHERE tenant_id = %s", (t,))
        except Exception:
            pass
    pool.close()


def _tenant(env, prefix: str) -> str:
    t = f"calib-{prefix}-{uuid.uuid4().hex[:8]}"
    env.tenants.append(t)
    return t


def _agent_key() -> str:
    return "ak_" + uuid.uuid4().hex + uuid.uuid4().hex  # >16 chars, unique


def _seed_gate_config(pool, tenant_id: str, tau: float, cap_k: float, enabled: bool) -> None:
    from aml.cgr.gate import calibration_tenant_tx
    with calibration_tenant_tx(pool, tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cgr_gate_config (tenant_id, tau, cap_k, enabled) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET tau=EXCLUDED.tau, cap_k=EXCLUDED.cap_k, "
            "enabled=EXCLUDED.enabled, updated_at=now()",
            (tenant_id, tau, cap_k, enabled))


# ── happy path: the CONVERTED write + read paths function and turn the gate ON ──
def test_write_then_gate_resolves_active(env):
    """Real `_write_calibration_audited` (transaction-local) persists a weight and its
    audit breadcrumb atomically; real `_resolve_gate_for` reads it back and builds an
    ACTIVE gate that ramps the calibrated source and floors unknown/thin ones."""
    from aml.cgr.routes import _write_calibration_audited
    from aml.cgr.engine import _resolve_gate_for
    from aml.cgr.gate import review_gate_g

    A = _tenant(env, "A")
    ak = _agent_key()
    _seed_gate_config(env.pool, A, tau=0.10, cap_k=3.0, enabled=True)

    ref = _write_calibration_audited(env.pool, env.gcrumbs, A, ak, 0.9, 12, "beta", "svc-key-1")
    assert ref, "write must return a breadcrumb id (audit trail persisted)"

    gate, cap_k = _resolve_gate_for(SimpleNamespace(_pool=env.pool), A)
    assert gate is not None and cap_k == 3.0                 # gate is ACTIVE
    assert abs(gate(ak) - review_gate_g(0.9, 0.10)) < 1e-9   # calibrated source ramps
    assert gate("some-unknown-source") == 0.0                # cold-start fail-safe floors it


def test_gate_neutral_without_calibration(env):
    """A tenant with an enabled config but zero calibration rows ⇒ NEUTRAL (byte-identical
    to v1). Confirms the read path doesn't turn the gate on by accident."""
    from aml.cgr.engine import _resolve_gate_for
    B = _tenant(env, "B")
    _seed_gate_config(env.pool, B, tau=0.10, cap_k=3.0, enabled=True)
    assert _resolve_gate_for(SimpleNamespace(_pool=env.pool), B) == (None, None)


# ── RLS row-isolation + no-leak: the load-bearing security assertion ──
def test_rls_isolation_and_no_guc_leak(env):
    enforceable, why = _rls_enforceable("agent_calibration")
    if not enforceable:
        pytest.skip(f"RLS not enforceable for this DB role ({why}) — calibration isolation "
                    f"MUST be verified on staging under grafomem_rt (NOSUPERUSER/NOBYPASSRLS). "
                    f"Not silently passing.")

    import psycopg
    from aml.cgr.routes import _write_calibration_audited
    from aml.cgr.engine import _resolve_gate_for
    from aml.cgr.gate import calibration_tenant_tx

    A = _tenant(env, "A")
    B = _tenant(env, "B")
    ak_a = _agent_key()
    _seed_gate_config(env.pool, A, tau=0.10, cap_k=3.0, enabled=True)
    _write_calibration_audited(env.pool, env.gcrumbs, A, ak_a, 0.9, 5, "beta", "svc-A")  # borrow+return

    # A reads its own row back through the real path
    gate_a, _ = _resolve_gate_for(SimpleNamespace(_pool=env.pool), A)
    assert gate_a is not None and gate_a(ak_a) > 0.0

    # (1) cross-tenant via the real engine path: B sees no config/calibration ⇒ neutral
    assert _resolve_gate_for(SimpleNamespace(_pool=env.pool), B) == (None, None)

    # (2) cross-tenant via a BARE SELECT (no WHERE) under B's GUC — only RLS can filter.
    #     Reuses the SAME max_size=1 physical connection A just used ⇒ also a leak probe.
    with calibration_tenant_tx(env.pool, B) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM agent_calibration")     # no WHERE tenant_id
        assert cur.fetchone()["n"] == 0, "B must see ZERO of A's calibration rows (RLS)"

    # (3) fail-closed: borrow with NO GUC at all ⇒ zero rows (and proves no A-GUC residue)
    raw = env.pool.getconn()
    try:
        with raw.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_tenant', true) AS t")
            assert cur.fetchone()["t"] in (None, ""), "tenant GUC leaked across pool borrow"
            cur.execute("SELECT count(*) AS n FROM agent_calibration")
            assert cur.fetchone()["n"] == 0, "no-GUC read must be empty (fail-closed)"
        raw.rollback()
    finally:
        env.pool.putconn(raw)

    # (4) A can still see exactly its own row (isolation is symmetric, bare SELECT)
    with calibration_tenant_tx(env.pool, A) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM agent_calibration")     # no WHERE tenant_id
        assert cur.fetchone()["n"] == 1

    # (5) cross-tenant WITH CHECK write, ROLLED BACK: under B's GUC, inserting a row that
    #     claims tenant A must be rejected by the policy (nothing persists).
    conn = env.pool.getconn()
    try:
        rejected = False
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT set_config('app.current_tenant', %s, true)", (B,))
                    cur.execute(
                        "INSERT INTO agent_calibration (tenant_id, agent_key, calibration_weight) "
                        "VALUES (%s,%s,%s)", (A, _agent_key(), 0.5))  # tenant_id=A while GUC=B
        except psycopg.errors.InsufficientPrivilege:
            rejected = True          # 42501 — Postgres surfaces a WITH CHECK violation this way
        assert rejected, "writing tenant A's row under B's GUC must be rejected by WITH CHECK"
    finally:
        env.pool.putconn(conn)

    # A's committed row is intact after all the cross-tenant attempts
    with calibration_tenant_tx(env.pool, A) as conn, conn.cursor() as cur:
        cur.execute("SELECT calibration_weight AS w FROM agent_calibration WHERE agent_key = %s", (ak_a,))
        assert abs(cur.fetchone()["w"] - 0.9) < 1e-9
