"""WorldModelService tenant GUC-scoping (RLS Stage 1 prerequisite).

Every world-model DB op runs inside a transaction whose `app.current_tenant` GUC is
TRANSACTION-LOCAL (`set_config(..., is_local=True)`), so Postgres resets it at
commit/rollback and it can never leak to the next borrower of a pooled connection.
These tests exercise that discipline with RLS still OFF — the `WHERE tenant_id=%s`
filters do the isolation today, and this GUC discipline is what will make the future
RLS policy safe. The leakage test is the standing guard for both.
"""
import os
import uuid

import pytest

TEST_DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


def _svc_and_pool():
    try:
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row
    except Exception:
        pytest.skip("psycopg_pool not installed")
    from aml.cloud.world_model import WorldModelService
    # max_size=1 ⇒ the SAME physical connection is reused across borrows, so a leaked
    # session GUC would be observable on the next borrow. row_factory=dict_row matches prod.
    try:
        pool = ConnectionPool(TEST_DB_URL, min_size=1, max_size=1,
                              kwargs={"row_factory": dict_row}, open=True)
    except Exception as e:
        pytest.skip(f"cannot open pool: {e}")
    svc = WorldModelService(TEST_DB_URL, pool=pool)  # signing_identity=None ⇒ unsigned rows (fine here)
    try:
        svc.ensure_schema()
    except Exception as e:
        pool.close(); pytest.skip(f"schema unavailable: {e}")
    return svc, pool


def _obj(props=None):
    return {"properties": props or {}}


def test_tenant_isolation_via_where_filter():
    svc, pool = _svc_and_pool()
    try:
        A = f"guc-A-{uuid.uuid4().hex[:8]}"
        B = f"guc-B-{uuid.uuid4().hex[:8]}"
        svc.register_type(A, "object", "Alpha", _obj())
        svc.register_type(A, "object", "AlphaTwo", _obj())
        svc.register_type(B, "object", "Beta", _obj())

        a_names = {t["name"] for t in svc.list_types(A)}
        b_names = {t["name"] for t in svc.list_types(B)}
        assert {"Alpha", "AlphaTwo"} <= a_names and "Beta" not in a_names
        assert "Beta" in b_names and "Alpha" not in b_names
        # get_type is tenant-scoped: A's type_id is not visible to B
        a_type = svc.get_type_by_name(A, "object", "Alpha")
        assert svc._get_type(B, type_id=a_type["type_id"]) is None
    finally:
        pool.close()


def test_guc_is_transaction_local_and_does_not_leak():
    svc, pool = _svc_and_pool()
    try:
        A = f"guc-A-{uuid.uuid4().hex[:8]}"
        # inside the tenant-tx the GUC is set to A
        with svc._tenant_tx(A) as cur:
            cur.execute("SELECT current_setting('app.current_tenant', true) AS t")
            assert cur.fetchone()["t"] == A
        # after the tx ends, borrow the SAME pooled connection and confirm the GUC
        # was reset (transaction-local) — it must NOT still be A.
        conn = pool.getconn()
        try:
            with conn.cursor() as c:
                c.execute("SELECT current_setting('app.current_tenant', true) AS t")
                leaked = c.fetchone()["t"]
        finally:
            pool.putconn(conn)
        assert leaked in (None, ""), f"GUC leaked across pool borrow: {leaked!r}"
        assert leaked != A
    finally:
        pool.close()


def test_no_cross_tenant_guc_on_pool_reuse():
    """Operate as A, return the connection, then operate as B on the SAME pooled
    connection — B's ops carry B's GUC (or none between ops), never A's residue."""
    svc, pool = _svc_and_pool()
    try:
        A = f"guc-A-{uuid.uuid4().hex[:8]}"
        B = f"guc-B-{uuid.uuid4().hex[:8]}"
        svc.register_type(A, "object", "Alpha", _obj())      # borrow #1 (tenant A), returned
        # between operations the pooled connection carries no residual tenant GUC
        conn = pool.getconn()
        try:
            with conn.cursor() as c:
                c.execute("SELECT current_setting('app.current_tenant', true) AS t")
                assert c.fetchone()["t"] in (None, "")
        finally:
            pool.putconn(conn)
        svc.register_type(B, "object", "Beta", _obj())       # borrow #2 (tenant B)
        # B cannot see A's rows; A cannot see B's — isolation intact under reuse
        assert "Alpha" not in {t["name"] for t in svc.list_types(B)}
        assert "Beta" not in {t["name"] for t in svc.list_types(A)}
    finally:
        pool.close()


def test_invoke_action_emits_receipt_in_tenant_tx():
    """The other multi-statement write path: invoke_action → _emit_receipt does its
    INSERT + read-back inside one tenant-tx and lists back under the same tenant."""
    from aml.cloud.world_model import ActionInvocation
    svc, pool = _svc_and_pool()
    try:
        A = f"guc-A-{uuid.uuid4().hex[:8]}"
        svc.register_type(A, "action", "certify",
                          {"operation": "worldmodel.action.certify", "required_trust_tier": "untrusted"})
        inv = ActionInvocation(action_name="certify", subject_refs=["INV-1"],
                               authority={"trust_tier": "untrusted", "human_principal": "tester"})
        rec = svc.invoke_action(A, inv)
        assert rec["action_name"] == "certify" and rec["status"] == "invoked"
        assert rec["action_id"] in {a["action_id"] for a in svc.list_actions(A)}
        # scoped: another tenant sees none of A's invocations
        B = f"guc-B-{uuid.uuid4().hex[:8]}"
        assert svc.list_actions(B) == []
    finally:
        pool.close()


def test_register_type_read_modify_write_is_atomic():
    """register_type upserts + reads back in ONE tenant-tx and returns the row."""
    svc, pool = _svc_and_pool()
    try:
        A = f"guc-A-{uuid.uuid4().hex[:8]}"
        row = svc.register_type(A, "object", "Alpha", _obj({"x": {"type": "string"}}))
        assert row["name"] == "Alpha" and row["tenant_id"] == A and row["kind"] == "object"
        # idempotent re-register (ON CONFLICT) returns the updated row, same type_id
        row2 = svc.register_type(A, "object", "Alpha", _obj({"x": {"type": "number"}}))
        assert row2["type_id"] == row["type_id"]
        assert row2["spec"]["properties"]["x"]["type"] == "number"
    finally:
        pool.close()
