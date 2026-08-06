"""Phase 2, F2 + F3 — attest→execute round-trip.

Drives the REAL attest handler with a real Ed25519 approver signature and asserts that an
APPROVE executes exactly the SIGNED action (parsed from context_bytes, F2) via the
deterministic executor (PR-6, execute_approved_action) — NOT via resume_workflow's LLM re-run.

Mock DB/orchestrator (no live PG needed). The execute_step PAUSE half of F3 (a governed
send_email escalating mid-workflow) needs a mock-LLM staging harness — flagged in the review
package; this covers the security-critical approve→execute path.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from aml.cloud.hitl_routes import create_hitl_router

PROPOSED = {"tool": "send_email", "to": "ana@globex.example",
            "args": {"to": "ana@globex.example", "subject": "hi"}, "invoice_ref": "OUT-globex-ana"}


class _Orch:
    def __init__(self):
        self.executed = []
        self.resumed = []

    def execute_approved_action(self, tenant_id, workflow_id, proposed_action):
        self.executed.append((tenant_id, workflow_id, proposed_action))
        return {"executed": True}

    def resume_workflow(self, workflow_id, approved):
        self.resumed.append((workflow_id, approved))


class _Gcrumbs:
    def append_breadcrumb(self, tenant_id, event_type, payload, conn=None):
        pass


class _Cur:
    def __init__(self, conn, q):
        self.conn, self.q = conn, q

    def fetchone(self):
        if "FROM hitl_approval_requests" in self.q and "FOR UPDATE" in self.q:
            return self.conn.row
        if "FROM hitl_approvers" in self.q:
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


def _make_key():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
    return priv, pub_hex


def _setup(proposed_action):
    priv, pub_hex = _make_key()
    ctx = {"request_id": "r1", "tenant_id": "corp", "workflow_id": "wf1", "step_id": "s1",
           "action": "send_email", "resource": "ana@globex.example"}
    if proposed_action is not None:
        ctx["proposed_action"] = proposed_action
    context_bytes = json.dumps(ctx).encode("utf-8")
    row = {"request_id": "r1", "status": "pending",
           "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
           "tenant_id": "corp", "workflow_id": "wf1", "step_id": "s1",
           "context_bytes": context_bytes, "context_json": ctx}
    orch = _Orch()
    router = create_hitl_router(_Pool(_Conn(row, pub_hex)), orch, _Gcrumbs())
    app = FastAPI(); app.include_router(router)
    return TestClient(app), priv, pub_hex, context_bytes, orch


def _attest(client, pub_hex, priv, context_bytes, decision):
    prefix = b"grafomem.hitl.approval.v1:"
    sig = priv.sign(prefix + context_bytes + b"\x1f" + decision.encode()).hex()
    return client.post("/v1/hitl/requests/r1/attest",
                       json={"decision": decision, "signer_id": pub_hex, "signature": sig})


def test_approve_executes_the_signed_action_deterministically():
    client, priv, pub_hex, cb, orch = _setup(PROPOSED)
    r = _attest(client, pub_hex, priv, cb, "approve")
    assert r.status_code == 200, r.text
    # PR-6: executed the committed action; F2: it came from the SIGNED context_bytes
    assert orch.executed == [("corp", "wf1", PROPOSED)]
    assert orch.resumed == []                       # NOT the LLM re-run path


def test_deny_does_not_execute():
    client, priv, pub_hex, cb, orch = _setup(PROPOSED)
    r = _attest(client, pub_hex, priv, cb, "deny")
    assert r.status_code == 200, r.text
    assert orch.executed == []                       # no execution on deny
    assert orch.resumed == [("wf1", False)]          # deny → resume(reject) path


def test_bad_signature_never_executes():
    client, priv, pub_hex, cb, orch = _setup(PROPOSED)
    other_priv, _ = _make_key()                      # sign with the WRONG key
    prefix = b"grafomem.hitl.approval.v1:"
    sig = other_priv.sign(prefix + cb + b"\x1f" + b"approve").hex()
    r = client.post("/v1/hitl/requests/r1/attest",
                    json={"decision": "approve", "signer_id": pub_hex, "signature": sig})
    assert r.status_code == 401
    assert orch.executed == [] and orch.resumed == []


def test_legacy_step_request_uses_resume_not_execute():
    # no proposed_action in the signed context → falls back to resume_workflow (unchanged)
    client, priv, pub_hex, cb, orch = _setup(None)
    r = _attest(client, pub_hex, priv, cb, "approve")
    assert r.status_code == 200, r.text
    assert orch.executed == []
    assert orch.resumed == [("wf1", True)]
