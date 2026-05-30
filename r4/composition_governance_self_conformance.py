#!/usr/bin/env python3
"""
tests/composition_governance_self_conformance.py — two-sided suite for Composition Governance (R4).

In-process against the live service + real Postgres (GRAFOMEM_DB_URL). Non-vacuous: composes
well-formed sets of certified, license-compatible members and REFUSES uncertified members,
incompatible licenses, and under-authorized composers; proves the receipt is tamper-evident on
both surfaces.

    GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem \
        python3 tests/composition_governance_self_conformance.py
"""
from __future__ import annotations
import os, sys, uuid

from aml.cloud.composition_governance import (
    CompositionGovernanceService, ComposeRequest, CompositionError, ComposeRejected,
    ComposePendingHITL, compute_composition_id, licenses_compatible, b2_256,
)
from psycopg.types.json import Jsonb

DB = os.environ.get("GRAFOMEM_DB_URL")
TENANT = f"r4conf-{uuid.uuid4().hex[:8]}"


class _Log:
    def __init__(self, result): self.result = result

class _Gateway:
    def __init__(self, allowed, result=None): self._a, self._r = allowed, result
    def evaluate_and_gate(self, tenant, operation, context):
        return (self._a, [] if self._a else [_Log(self._r)])


def good_members():
    return [{"ref_id": "cert-aaa", "license": "CC-BY-4.0", "certified": True},
            {"ref_id": "cert-bbb", "license": "Apache-2.0", "certified": True}]

def good_request(kind="lora-stack", target="oci://acme/composed:1") -> ComposeRequest:
    return ComposeRequest(composition_kind=kind, members=good_members(), target_ref=target,
                          authority={"delegation_ref": "gns://acme/cert/9", "human_principal": "camilo@acme",
                                     "trust_tier": "release"}, required_trust_tier="verified")


def main() -> int:
    if not DB:
        print("ERROR: set GRAFOMEM_DB_URL"); return 2

    key = os.urandom(32)
    svc = CompositionGovernanceService(DB, signing_key=key)
    svc.ensure_schema()

    results, st = [], {}

    def gate(name, fn):
        try:
            fn(); results.append((name, True, ""))
        except AssertionError as e:
            results.append((name, False, str(e) or "assertion failed"))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))

    def k1():
        c = svc.compose(TENANT, good_request())
        assert c.get("composition_id"), "no composition_id"
        st["cid"] = c["composition_id"]
    gate("K1  govern a clean composition", k1)

    def k2():
        row = svc.get(TENANT, st["cid"])
        assert row.get("document"), "no signed receipt stored"
        assert len(row["document"]["members"]) == 2, "members lost"
    gate("K2  persistence round-trip", k2)

    def k3():
        v = svc.verify(TENANT, st["cid"])
        assert v["passed"], f"authentic receipt failed verify: {v['checks']}"
        assert all(v["checks"].values()), v["checks"]
    gate("K3  composition receipt signed + verifies", k3)

    def k4():
        # dedicated composition so this gate is order-independent
        cid = svc.compose(TENANT, good_request(target="oci://acme/tamper:1"))["composition_id"]
        # (a) tamper a member -> caught by the members-digest binding (not the signature)
        doc = svc.get(TENANT, cid)["document"]; doc["members"][0]["license"] = "TAMPERED"
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("UPDATE compositions SET document=%s WHERE composition_id=%s", (Jsonb(doc), cid))
        v = svc.verify(TENANT, cid)
        assert not v["checks"]["members_consistent"], "member tamper not caught by members digest"
        assert not v["passed"], "tampered composition still passed"
        # (b) tamper a signed field -> caught by the signature
        doc["target_ref"] = "oci://evil/x:1"
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("UPDATE compositions SET document=%s WHERE composition_id=%s", (Jsonb(doc), cid))
        assert not svc.verify(TENANT, cid)["checks"]["signature"], "signed-field tamper not caught by signature"
    gate("K4  tamper-evidence: signature + members (non-vacuous)", k4)

    def k5():
        # content-addressed + order-independent identity
        a = compute_composition_id(TENANT, "lora-stack", ["cert-aaa", "cert-bbb"])
        b = compute_composition_id(TENANT, "lora-stack", ["cert-bbb", "cert-aaa"])  # different order
        assert a == b, "composition id not order-independent"
        assert len(a) == 32, f"id not BLAKE2b-128 (len={len(a)})"
    gate("K5  content-addressed order-independent id", k5)

    def k6():
        req = good_request(target="oci://acme/uncertified:1")
        req.members[1]["certified"] = False
        try:
            svc.compose(TENANT, req); assert False, "uncertified member composed"
        except ComposeRejected as e:
            assert any("not certified" in r for r in e.reasons), e.reasons
    gate("K6  refuse uncertified member (non-vacuous)", k6)

    def k7():
        # no-derivatives present
        nd = good_request(target="oci://acme/nd:1"); nd.members[0]["license"] = "CC-BY-ND-4.0"
        try:
            svc.compose(TENANT, nd); assert False, "no-derivatives license composed"
        except ComposeRejected:
            pass
        # non-commercial mixed with commercial
        mix = good_request(target="oci://acme/mix:1")
        mix.members[0]["license"] = "CC-BY-NC-4.0"; mix.members[1]["license"] = "Commercial-Redistribution"
        try:
            svc.compose(TENANT, mix); assert False, "NC+commercial composed"
        except ComposeRejected:
            pass
        # sanity: the compatible baseline is accepted
        ok, _ = licenses_compatible(["CC-BY-4.0", "Apache-2.0"])
        assert ok, "compatible licenses wrongly rejected"
    gate("K7  refuse incompatible licenses (non-vacuous)", k7)

    def k8():
        under = good_request(target="oci://acme/under:1")
        under.authority["trust_tier"] = "basic"        # below required 'verified'
        try:
            svc.compose(TENANT, under); assert False, "under-authorized composer allowed"
        except ComposeRejected as e:
            assert any("trust tier" in r for r in e.reasons), e.reasons
        meets = good_request(target="oci://acme/meets:1"); meets.authority["trust_tier"] = "verified"
        assert svc.compose(TENANT, meets).get("composition_id"), "meeting-tier composer rejected"
    gate("K8  composer authority gate (non-vacuous)", k8)

    def k9():
        denier = CompositionGovernanceService(DB, signing_key=key, gateway=_Gateway(False, "denied"))
        try:
            denier.compose(TENANT, good_request(target="oci://acme/gov-deny:1")); assert False, "deny not enforced"
        except ComposeRejected:
            pass
        hitl = CompositionGovernanceService(DB, signing_key=key, gateway=_Gateway(False, "escalated"))
        try:
            hitl.compose(TENANT, good_request(target="oci://acme/hitl:1")); assert False, "escalation not parked"
        except ComposePendingHITL as e:
            pending = e.composition_id
        rec = hitl.resume(TENANT, pending, approved=True, approver="approver@acme")
        assert hitl.verify(TENANT, rec["composition_id"])["passed"], "resumed composition fails verify"
    gate("K9  governance deny + HITL resume", k9)

    def k10():
        art = svc.composed_artifact(TENANT, st["cid"])
        assert art["artifact_ref"] and art["members"] and art["kind"], "composed artifact descriptor incomplete"
        assert "cert-aaa" in art["members"], "members not exposed for R1 registration"
    gate("K10 R4->R1 composed-artifact descriptor", k10)

    try:
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM compositions WHERE tenant_id=%s", (TENANT,))
    except Exception:
        pass

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n  composition-governance self-conformance  (tenant {TENANT})\n  " + "-" * 56)
    for name, ok, msg in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        -> {msg}" if not ok else ""))
    print("  " + "-" * 56)
    print(f"  {passed}/{len(results)} gates green\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
