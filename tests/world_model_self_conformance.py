#!/usr/bin/env python3
"""
tests/world_model_self_conformance.py — two-sided suite for the Governed World-Model (R5).

In-process against the live service + real Postgres (GRAFOMEM_DB_URL). Non-vacuous throughout:
the model ACCEPTS well-formed types/instances/actions and REJECTS malformed schemas, bad
instances, mismatched links, tampered receipts, under-authorized callers, and denied actions.

    GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem \
        python3 tests/world_model_self_conformance.py
"""
from __future__ import annotations
import os, sys, uuid

from aml.cloud.world_model import (
    WorldModelService, ActionInvocation, WorldModelError, ActionDenied, ActionPendingHITL,
    compute_type_id,
)
from psycopg.types.json import Jsonb

DB = os.environ.get("GRAFOMEM_DB_URL")
TENANT = f"wmconf-{uuid.uuid4().hex[:8]}"


class _Log:
    def __init__(self, result): self.result = result

class _Gateway:
    def __init__(self, allowed, result=None): self._a, self._r = allowed, result
    def evaluate_and_gate(self, tenant, operation, context):
        return (self._a, [] if self._a else [_Log(self._r)])


def main() -> int:
    if not DB:
        print("ERROR: set GRAFOMEM_DB_URL"); return 2

    key = os.urandom(32)
    svc = WorldModelService(DB, signing_identity=_MockId(key))        # gateway=None baseline
    svc.ensure_schema()

    results, st = [], {}

    def gate(name, fn):
        try:
            fn(); results.append((name, True, ""))
        except AssertionError as e:
            results.append((name, False, str(e) or "assertion failed"))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))

    # W1 — register Object types
    def w1():
        acct = svc.register_type(TENANT, "object", "Account",
            {"properties": {"id": {"type": "string", "required": True},
                            "balance": {"type": "number", "required": True},
                            "frozen": {"type": "boolean"}}})
        svc.register_type(TENANT, "object", "Counterparty",
            {"properties": {"id": {"type": "string", "required": True}}})
        assert acct["type_id"] == compute_type_id(TENANT, "object", "Account"), "type_id not deterministic"
        st["acct_tid"] = acct["type_id"]
    gate("W1  register object types (deterministic id)", w1)

    # W2 — register Link type; references must resolve (non-vacuous)
    def w2():
        svc.register_type(TENANT, "link", "pays",
            {"from_type": "Account", "to_type": "Counterparty", "cardinality": "many"})
        try:
            svc.register_type(TENANT, "link", "bogus", {"from_type": "Account", "to_type": "Nonexistent"})
            assert False, "link referencing unknown object type accepted"
        except WorldModelError:
            pass
    gate("W2  register link type + reject dangling reference", w2)

    # W3 — register Action type (operation + required trust tier)
    def w3():
        svc.register_type(TENANT, "action", "approve_payment",
            {"operation": "worldmodel.action.approve_payment", "required_trust_tier": "verified",
             "input_schema": {"amount": {"type": "number", "required": True}}})
    gate("W3  register action type", w3)

    # W4 — type receipt signed + verifies
    def w4():
        v = svc.verify_type(TENANT, st["acct_tid"])
        assert v["passed"], f"type verify failed: {v['checks']}"
        assert v["checks"]["signature"] and v["checks"]["schema_consistent"], v["checks"]
    gate("W4  type receipt signed + verifies", w4)

    # W5 — tamper-evidence on a type receipt (non-vacuous)
    def w5():
        row = svc.get_type(TENANT, st["acct_tid"]); doc = row["document"]
        doc["spec"]["properties"]["balance"]["required"] = False        # tamper a signed field
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("UPDATE world_model_types SET document=%s WHERE type_id=%s", (Jsonb(doc), st["acct_tid"]))
        v = svc.verify_type(TENANT, st["acct_tid"])
        assert not v["checks"]["signature"], "tampered type receipt still verified — signature vacuous!"
        assert not v["passed"], "tampered type still passed"
    gate("W5  type tamper-evidence (non-vacuous)", w5)

    # W6 — object instance validation (non-vacuous)
    def w6():
        ok = svc.validate_object(TENANT, "Counterparty", {"id": "cp-1"})
        assert ok["valid"], f"valid instance rejected: {ok['errors']}"
        missing = svc.validate_object(TENANT, "Counterparty", {})
        assert not missing["valid"], "missing required property accepted"
    gate("W6  object validation (non-vacuous)", w6)

    # W7 — link endpoint-type validation (non-vacuous)
    def w7():
        ok = svc.validate_link(TENANT, "pays", "Account", "Counterparty")
        assert ok["valid"], f"valid link rejected: {ok['errors']}"
        bad = svc.validate_link(TENANT, "pays", "Counterparty", "Account")
        assert not bad["valid"], "mismatched link endpoints accepted"
    gate("W7  link validation (non-vacuous)", w7)

    # W8 — governed action invoke -> signed, attributable receipt
    def w8():
        rec = svc.invoke_action(TENANT, ActionInvocation(
            action_name="approve_payment", subject_refs=["Account:a1", "Counterparty:cp-1"],
            params={"amount": 100.0},
            authority={"delegation_ref": "gns://acme/cert/1", "human_principal": "camilo@acme",
                       "trust_tier": "release"}))
        v = svc.verify_action(TENANT, rec["action_id"])
        assert v["passed"], f"invocation receipt fails verify: {v['checks']}"
        assert v["attribution"]["did"] == "approve_payment", "attribution wrong"
    gate("W8  governed action invoke -> verifiable receipt", w8)

    # W9 — governance deny + HITL escalate/resume
    def w9():
        denier = WorldModelService(DB, signing_identity=_MockId(key), gateway=_Gateway(False, "denied"))
        try:
            denier.invoke_action(TENANT, ActionInvocation("approve_payment", ["Account:a1"],
                {"amount": 1.0}, {"trust_tier": "root"})); assert False, "governance deny not enforced"
        except ActionDenied:
            pass
        hitl = WorldModelService(DB, signing_identity=_MockId(key), gateway=_Gateway(False, "escalated"))
        try:
            hitl.invoke_action(TENANT, ActionInvocation("approve_payment", ["Account:a1"],
                {"amount": 1.0}, {"trust_tier": "root"})); assert False, "escalation not parked"
        except ActionPendingHITL as e:
            pending = e.action_id
        rec = hitl.resume_action(TENANT, pending, approved=True, approver="approver@acme")
        assert hitl.verify_action(TENANT, rec["action_id"])["passed"], "resumed action fails verify"
    gate("W9  governance deny + HITL resume", w9)

    # W10 — AUTHORITY gate: trust tier below the action's requirement is refused (the crux)
    def w10():
        under = ActionInvocation("approve_payment", ["Account:a1"], {"amount": 1.0}, {"trust_tier": "basic"})
        try:
            svc.invoke_action(TENANT, under); assert False, "under-authorized caller allowed"
        except ActionDenied as e:
            assert "trust tier" in e.reason, f"wrong denial reason: {e.reason}"
        # exactly-meeting tier is allowed
        meets = ActionInvocation("approve_payment", ["Account:a1"], {"amount": 1.0}, {"trust_tier": "verified"})
        rec = svc.invoke_action(TENANT, meets)
        assert svc.verify_action(TENANT, rec["action_id"])["passed"], "meeting-tier invocation failed"
    gate("W10 trust-tier authority gate (non-vacuous)", w10)

    # cleanup
    try:
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM world_model_actions WHERE tenant_id=%s", (TENANT,))
            cur.execute("DELETE FROM world_model_types   WHERE tenant_id=%s", (TENANT,))
    except Exception:
        pass

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n  world-model self-conformance  (tenant {TENANT})\n  " + "-" * 56)
    for name, ok, msg in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        -> {msg}" if not ok else ""))
    print("  " + "-" * 56)
    print(f"  {passed}/{len(results)} gates green\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())


class _MockId:
    def __init__(self, k): self.k = k
    def sign(self, m): 
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        priv = Ed25519PrivateKey.from_private_bytes(self.k)
        return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return Ed25519PrivateKey.from_private_bytes(self.k).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
