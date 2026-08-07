"""Governed dev loop v0 — the Cowork Step-0 review's mandated test suite.

Proves the v0 honors the 4 conditions (docs/cgr/track2-governed-dev-loop-v0.md):

  1. HARD invariant — propose ≠ attest: the bridge holds no approver key and cannot
     self-approve a consequential action (a bridge-held Ed25519 key never validates against
     the registered human approver's key). test_bridge_cannot_self_approve_*.
  2. All live CGR/RLS proofs run on DEDICATED TEST TENANTS, never corp.
  3. invoice_ref-as-work-item-id + receivables-vocab reuse (the v0 shortcut).
  4. Binary outcome mapping — compute_scores scores ONLY paid(+)/default(-); map_dev_outcome
     makes the positive/negative assignment explicit and returns None for the ambiguous.

Plus: consequential→escalates+HITL+not-executed; low-risk→recorded+no-HITL; the policy matches
the right tool names; a CGR delta on eng-agent after an outcome; and the per-eng-agent RLS
negative across two test tenants.

The pure/policy/attest tests are DB-free. The CGR-delta and RLS-negative tests use the local
test Postgres (conftest), same as tests/test_cgr_substrate.py / tests/test_rls_decision_hitl.py.
"""
from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ops.govern_dev import (
    CONSEQUENTIAL_TOOLS,
    DEV_HITL_POLICY,
    DEV_OUTCOME_MAP,
    LOW_RISK_TOOLS,
    GovernedDevBridge,
    map_dev_outcome,
)

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"
ENG_KEY = "e" * 64
ENG_HANDLE = "eng-agent@ulissy"


# ============================================================================
# 1. Policy matches the right tool names — real PolicyEngine + DEV_HITL_POLICY.
# ============================================================================

def _dev_policy():
    from aml.cloud.governance import Policy, PolicyAction, PolicyType
    return Policy(
        policy_id="dev-hitl", tenant_id="t", name=DEV_HITL_POLICY["name"], description="",
        policy_type=PolicyType.HITL_REQUIRED, action=PolicyAction.ESCALATE,
        config=DEV_HITL_POLICY["config"],
    )


def test_policy_escalates_exactly_the_consequential_tools():
    from aml.cloud.governance import EvaluationResult
    from aml.cloud.policy_engine import PolicyEngine
    engine, policy = PolicyEngine(), _dev_policy()

    for tool in CONSEQUENTIAL_TOOLS:
        v = engine.evaluate_single(policy, "tool_execution", {"tool_name": tool})
        assert v.result == EvaluationResult.ESCALATED, f"{tool} must escalate to HITL"
    for tool in LOW_RISK_TOOLS:
        v = engine.evaluate_single(policy, "tool_execution", {"tool_name": tool})
        assert v.result == EvaluationResult.ALLOWED, f"{tool} must be allowed (record, no HITL)"
    # a tool outside the dev set is not caught by this policy either
    assert engine.evaluate_single(policy, "tool_execution",
                                  {"tool_name": "rm_rf_prod"}).result == EvaluationResult.ALLOWED
    # exact set — no drift between the policy config and the taxonomy
    assert set(DEV_HITL_POLICY["config"]["tools"]) == set(CONSEQUENTIAL_TOOLS)


# ============================================================================
# 2/3. propose_action: consequential → escalates + HITL + NOT executed;
#      low-risk → recorded, no HITL. Drives the REAL policy through propose_action.
# ============================================================================

from datetime import datetime, timezone

from aml.cloud.orchestrator import AgentDefinition, OrchestratorService

_NOW = datetime(2026, 8, 7, tzinfo=timezone.utc)


class _FakeRec:
    decision_id = "dec-eng-1"
    tenant_id = "devtest"
    created_at = _NOW


class _FakeTrail:
    def __init__(self):
        self.calls = []

    def log(self, **kw):
        self.calls.append(kw)
        return _FakeRec()


class _DevGov:
    """The REAL PolicyEngine + DEV_HITL_POLICY, adapted to the governance interface
    propose_action calls. Exercises the actual policy config end-to-end (not a hand fake)."""

    def __init__(self):
        from aml.cloud.policy_engine import PolicyEngine
        self._engine, self._policy = PolicyEngine(), _dev_policy()

    def evaluate_and_gate(self, tenant, op, ctx):
        from aml.cloud.governance import EvaluationResult
        v = self._engine.evaluate_single(self._policy, op, ctx)
        allowed = v.result not in (EvaluationResult.DENIED, EvaluationResult.ESCALATED)
        return allowed, [v]


def _eng_agent():
    return AgentDefinition(
        agent_id="eng-aid", tenant_id="devtest", name=ENG_HANDLE, role="executor",
        description="", model_id="claude-opus-4-8", fallback_models=[], system_prompt="",
        memory_stores=[], tools=[], max_steps=20, max_tokens_per_step=4096, temperature=0.7,
        enabled=True, created_at=_NOW, updated_at=_NOW, agent_key=ENG_KEY, agent_handle=ENG_HANDLE,
    )


def _orch_with_policy(monkeypatch):
    orch = OrchestratorService(db_url="", governance=_DevGov(), decision_trail=_FakeTrail(),
                               signing_identity=None)
    monkeypatch.setattr(orch, "get_agent", lambda aid, encryption=None: _eng_agent())
    created = []
    monkeypatch.setattr(orch, "_create_hitl_request",
                        lambda *a, **k: created.append((a, k)) or "hitl-eng-1")
    return orch, created


@pytest.mark.parametrize("tool", CONSEQUENTIAL_TOOLS)
def test_consequential_escalates_and_not_executed(monkeypatch, tool):
    orch, created = _orch_with_policy(monkeypatch)
    out = orch.propose_action("devtest", "eng-aid", tool, {"pr": "PR-42"}, "PR-42", reason="ship it")
    assert out["escalated"] is True, f"{tool} must escalate"
    assert out["hitl_request_id"] == "hitl-eng-1" and len(created) == 1  # HITL created
    assert out["executed"] is False                                       # NOT executed until approved
    assert out["decision"] == "certify" and out["proposed"] is True


@pytest.mark.parametrize("tool", LOW_RISK_TOOLS)
def test_low_risk_recorded_no_hitl(monkeypatch, tool):
    orch, created = _orch_with_policy(monkeypatch)
    out = orch.propose_action("devtest", "eng-aid", tool, {"pr": "PR-42"}, "PR-42")
    assert out["escalated"] is False and out["hitl_request_id"] is None
    assert created == []                                                  # no HITL
    assert out["proposed"] is True                                        # but recorded as a governed decision


# ============================================================================
# 4 (condition 1, HARD). propose ≠ attest — the bridge cannot self-approve.
#
# The attest handler verifies the signature against the REGISTERED approver's key. The bridge
# holds only propose/tenant creds and its own key — never the approver's private key — so any
# bridge-produced attest fails (401). A real, distinct approver CAN approve (proving it's the
# invariant, not a broken attest path).
# ============================================================================

from aml.cloud.hitl_routes import create_hitl_router

_PROPOSED = {"tool": "deploy", "to": None,
             "args": {"pr": "PR-42", "target": "prod"}, "invoice_ref": "PR-42"}


def _make_key():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
    return priv, pub_hex


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


class _Cur:
    def __init__(self, conn, q):
        self.conn, self.q = conn, q

    def fetchone(self):
        if "FROM hitl_approval_requests" in self.q and "FOR UPDATE" in self.q:
            return self.conn.row
        if "FROM hitl_approvers" in self.q:
            # the tenant's registered approver — a key the BRIDGE does not hold
            return {"public_key": self.conn.approver_pub}
        return None


class _Conn:
    def __init__(self, row, approver_pub):
        self.row, self.approver_pub = row, approver_pub

    def execute(self, q, p=()):
        return _Cur(self, q)


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        c = self._conn

        class _Ctx:
            def __enter__(self_):
                return c

            def __exit__(self_, *a):
                return False

        return _Ctx()


def _attest_client(approver_pub, workflow_id="propose:PR-42:dec-eng-1"):
    from datetime import timedelta
    ctx = {"request_id": "r1", "tenant_id": "devtest", "workflow_id": workflow_id,
           "step_id": "dec-eng-1", "action": "deploy", "resource": "prod",
           "proposed_action": _PROPOSED}
    cb = json.dumps(ctx).encode("utf-8")
    row = {"request_id": "r1", "status": "pending",
           "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
           "tenant_id": "devtest", "workflow_id": workflow_id, "step_id": "dec-eng-1",
           "context_bytes": cb, "context_json": ctx}
    orch = _Orch()
    router = create_hitl_router(_Pool(_Conn(row, approver_pub)), orch, _Gcrumbs())
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), orch, cb


def _sign(priv, cb, decision):
    return priv.sign(b"grafomem.hitl.approval.v1:" + cb + b"\x1f" + decision.encode()).hex()


def test_bridge_cannot_self_approve_consequential_action():
    approver_priv, approver_pub = _make_key()   # the human approver (out of band)
    bridge_priv, bridge_pub = _make_key()        # a key the BRIDGE controls
    client, orch, cb = _attest_client(approver_pub)

    # (a) bridge signs with its OWN key + its own signer_id → not the registered approver → 401
    r = client.post("/v1/hitl/requests/r1/attest",
                    json={"decision": "approve", "signer_id": bridge_pub, "signature": _sign(bridge_priv, cb, "approve")})
    assert r.status_code == 401, r.text
    assert orch.executed == []

    # (b) bridge IMPERSONATES the approver's signer_id but still can't produce the approver's
    #     signature (it lacks the private key) → 401
    r = client.post("/v1/hitl/requests/r1/attest",
                    json={"decision": "approve", "signer_id": approver_pub, "signature": _sign(bridge_priv, cb, "approve")})
    assert r.status_code == 401, r.text
    assert orch.executed == []

    # (c) the REAL approver (a DISTINCT signer, not the bridge) CAN approve → the deploy executes.
    #     Proves propose≠attest is the invariant, not a broken attest path.
    r = client.post("/v1/hitl/requests/r1/attest",
                    json={"decision": "approve", "signer_id": approver_pub, "signature": _sign(approver_priv, cb, "approve")})
    assert r.status_code == 200, r.text
    assert orch.executed == [("devtest", "propose:PR-42:dec-eng-1", _PROPOSED)]


def test_bridge_exposes_no_attest_or_approver_key():
    """Structural guard on condition 1: the bridge type has NO attest/approve/sign method and
    carries no approver key. If a future change adds one, this fails and forces a review."""
    bridge = GovernedDevBridge("https://x", "k", agent_id="a")
    for forbidden in ("attest", "approve", "sign", "self_approve"):
        assert not hasattr(bridge, forbidden), f"bridge must not expose .{forbidden}() (propose≠attest)"
    # no attribute holds an ed25519 private key
    assert not any("priv" in a or "approver" in a for a in vars(bridge)), vars(bridge)


# ============================================================================
# condition 4. Binary outcome mapping — explicit positive/negative; ambiguous → None.
# ============================================================================

def test_map_dev_outcome_binary_positive_negative():
    # positive dev results → 'paid' (α)
    for good in ("deploy_succeeded", "migration_applied", "ci_passed", "merge_landed", "pr_merged"):
        assert map_dev_outcome(good) == "paid", good
    # negative dev results → 'default' (β)
    for bad in ("deploy_failed", "deploy_rolled_back", "migration_failed", "ci_failed", "merge_reverted"):
        assert map_dev_outcome(bad) == "default", bad
    # ambiguous / in-flight / unknown → None (do NOT post — leave the decision pending)
    for amb in ("in_progress", "superseded", "flaky_retry", "", "unknown"):
        assert map_dev_outcome(amb) is None, amb
    # every mapped value is one of the two CGR-scored labels — nothing lands on an unscored label
    assert set(DEV_OUTCOME_MAP.values()) == {"paid", "default"}


def test_record_outcome_none_mapping_is_noop():
    """A dev result with no scored mapping must NOT post an outcome (would falsely resolve)."""
    bridge = GovernedDevBridge("https://x", "k", agent_id="a")
    res = bridge.record_outcome("PR-42", "in_progress")
    assert res["posted"] is False and "pending" in res["reason"]


# ============================================================================
# 5 (test tenant). CGR delta on eng-agent after a mapped outcome.
# ============================================================================

def test_cgr_delta_on_eng_agent_after_outcome():
    from aml.backends.interface import WriteOptions
    from aml.backends.postgres_gmp import PostgresGMPBackend
    from aml.cgr.engine import compute_scores
    from aml.cgr.substrate import CGR_OUTCOME_SCHEMA, CGR_OUTCOMES_STORE
    from aml.cloud.decision_trail import DecisionTrailService
    from aml.server.stores import StoreManager

    T = "devtest-" + uuid.uuid4().hex[:8]          # DEDICATED test tenant (condition 2)
    work = "PR-" + uuid.uuid4().hex[:8]            # the work-item id, used as invoice_ref (condition 3)

    dt = DecisionTrailService(TEST_DB_URL)
    dt.ensure_schema()
    # a proposed dev decision, shaped exactly as orchestrator.propose_action logs it
    dt.log(tenant_id=T, store_id="governed",
           query=json.dumps({"tool": "deploy", "invoice_ref": work}),
           model_id="claude-opus-4-8", raw_output=json.dumps({"decision": "certify"}),
           parameters={"invoice_ref": work, "invoice_id": work, "decision": "certify",
                       "agent_key": ENG_KEY, "agent_handle": ENG_HANDLE,
                       "verifiability_tag": "judgment", "cgr_schema": "cgr.decision.v1",
                       "tool": "deploy", "proposed": True})

    sm = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL))
    try:
        # baseline: one pending eng-agent decision, nothing resolved → neutral 0.5
        base = compute_scores(dt, sm, T)
        assert len(base) == 1 and base[0].agent_handle == ENG_HANDLE
        assert base[0].n_resolved == 0 and base[0].n_pending == 1
        assert base[0].cgr_score == pytest.approx(0.5)

        # a mapped SUCCESS outcome ('paid'); subject is the SAME work-item id (pure-equality join)
        outcome = map_dev_outcome("deploy_succeeded")
        assert outcome == "paid"
        ob = sm.get_or_create_named(CGR_OUTCOMES_STORE).backend
        meta = {"predicate": "receivable_outcome", "subject": work, "object": outcome,
                "cgr_schema": CGR_OUTCOME_SCHEMA, "source": "dev-loop"}
        ob.write(f"receivable_outcome | {work} | {outcome}", WriteOptions(tenant_id=T, metadata=meta))

        # the DELTA: the decision resolves and the score moves up off the neutral baseline
        after = compute_scores(dt, sm, T)
        assert len(after) == 1 and after[0].agent_handle == ENG_HANDLE
        assert after[0].n_resolved == 1 and after[0].n_pending == 0
        assert after[0].cgr_score > base[0].cgr_score          # 'paid' lifts the score
    finally:
        for e in list(getattr(sm, "_stores", {}).values()):
            try:
                e.backend.close()
            except Exception:
                pass


# ============================================================================
# 6 (two test tenants). Per-eng-agent RLS negative — eng-agent records are tenant-isolated.
#
# Mirrors tests/test_rls_decision_hitl.py: a self-provisioned NOSUPERUSER NOBYPASSRLS role +
# the FORCE-RLS tenant_isolation policy on the REAL decision_records table. Proves an eng-agent
# decision written under test-tenant A is invisible under test-tenant B's context (and unset ⇒
# fail-closed). No prod key needed — Track 1 covers the per-table policy; this is the per-agent
# flavor across two tenants.
# ============================================================================

from aml.server.tenant_context import apply_tenant_context, current_tenant

_OWNER_URL = TEST_DB_URL
_RT_ROLE = "dev_loop_rls_rt"
_RT_PW = "rtpw"
_RT_URL = f"postgresql://{_RT_ROLE}:{_RT_PW}@localhost:5432/grafomem"

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
    return psycopg.connect(_OWNER_URL, autocommit=True)


@pytest.fixture(scope="module")
def enforcing():
    """(url, role_or_None) for a connection RLS actually enforces against — a self-provisioned
    non-owner NOSUPERUSER NOBYPASSRLS role (CI), else the owner iff non-super/non-bypass, else
    skip (RLS inert under a superuser)."""
    with _owner() as c:
        sup, byp, cr = c.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
    if sup or cr:
        with _owner() as c:
            c.execute(f"DROP ROLE IF EXISTS {_RT_ROLE}")
            c.execute(f"CREATE ROLE {_RT_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{_RT_PW}'")
            c.execute(f"GRANT USAGE ON SCHEMA public TO {_RT_ROLE}")
            c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {_RT_ROLE}")
            c.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {_RT_ROLE}")
        yield _RT_URL, _RT_ROLE
        with _owner() as c:
            c.execute(f"REASSIGN OWNED BY {_RT_ROLE} TO grafomem")
            c.execute(f"DROP OWNED BY {_RT_ROLE}")
            c.execute(f"DROP ROLE IF EXISTS {_RT_ROLE}")
    elif not sup and not byp:
        yield _OWNER_URL, None
    else:
        pytest.skip("only a superuser/bypassrls role available — RLS inert; prove under a restricted role")


def _set_ctx(conn, tenant):
    tok = current_tenant.set(tenant)
    try:
        apply_tenant_context(conn)
    finally:
        current_tenant.reset(tok)


def test_per_eng_agent_records_isolated_across_two_test_tenants(enforcing):
    from aml.cloud.decision_trail import DecisionTrailService

    url, _role = enforcing
    A, B = f"engA-{uuid.uuid4().hex[:6]}", f"engB-{uuid.uuid4().hex[:6]}"
    dt = DecisionTrailService(_OWNER_URL)
    dt.ensure_schema()
    # one eng-agent decision under each test tenant (as owner, BEFORE the policy tightens)
    for T, ref in ((A, "PR-A"), (B, "PR-B")):
        dt.log(tenant_id=T, store_id="governed", query="{}", model_id="m", raw_output="{}",
               parameters={"invoice_ref": ref, "agent_key": ENG_KEY, "agent_handle": ENG_HANDLE,
                           "verifiability_tag": "judgment", "cgr_schema": "cgr.decision.v1"})

    pol = "iso_devloop_decision_records"
    q_eng = ("SELECT parameters->>'invoice_ref' FROM decision_records "
             "WHERE parameters->>'agent_handle' = %s")
    with _owner() as c:
        had = c.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname='decision_records'").fetchone()
        c.execute(_POLICY.format(t="decision_records", p=pol))
    try:
        with psycopg.connect(url) as rc:
            _set_ctx(rc, A)
            refs_a = {r[0] for r in rc.execute(q_eng, (ENG_HANDLE,)).fetchall()}
            assert "PR-A" in refs_a and "PR-B" not in refs_a   # A sees only A's eng-agent record

            _set_ctx(rc, B)
            refs_b = {r[0] for r in rc.execute(q_eng, (ENG_HANDLE,)).fetchall()}
            assert "PR-B" in refs_b and "PR-A" not in refs_b   # the per-eng-agent NEGATIVE: B never sees A

            _set_ctx(rc, None)                                  # unset ⇒ fail-closed
            assert rc.execute("SELECT count(*) FROM decision_records").fetchone()[0] == 0
    finally:
        with _owner() as c:
            c.execute(f"DROP POLICY IF EXISTS {pol} ON decision_records")
            if had and not had[1]:
                c.execute("ALTER TABLE decision_records NO FORCE ROW LEVEL SECURITY")
            if had and not had[0]:
                c.execute("ALTER TABLE decision_records DISABLE ROW LEVEL SECURITY")
