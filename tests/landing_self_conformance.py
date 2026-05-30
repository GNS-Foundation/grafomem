#!/usr/bin/env python3
"""
tests/landing_self_conformance.py — two-sided suite for the Landing Certificate service.

Runs in-process against the live LandingService + your real Postgres (GRAFOMEM_DB_URL),
mirroring tests/gmp_self_conformance.py. Every gate is non-vacuous: the suite proves the
service both ACCEPTS valid certificates and REJECTS tampered / unauthorized / unsealed ones.

    GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem \
        python tests/landing_self_conformance.py
"""
from __future__ import annotations
import os, sys, uuid

from aml.cloud.landing_service import (
    LandingService, LandingIssueRequest, LandingError, LandingDenied, LandingPendingHITL,
    compute_certificate_id, b2_256,
)
from psycopg.types.json import Jsonb

DB = os.environ.get("GRAFOMEM_DB_URL")
TENANT = f"selfconf-{uuid.uuid4().hex[:8]}"


# --- stub gateway: returns (allowed, logs) exactly like GovernanceGateway.evaluate_and_gate ---
class _Log:
    def __init__(self, result): self.result = result

class _Gateway:
    def __init__(self, allowed: bool, result: str | None = None):
        self._allowed, self._result = allowed, result
    def evaluate_and_gate(self, tenant, operation, context):
        return (self._allowed, [] if self._allowed else [_Log(self._result)])


def good_request() -> LandingIssueRequest:
    layer = b"adapter-weights-bytes-v1"
    return LandingIssueRequest(
        artifact_ref="oci://registry/acme/util-lora:1.0",
        base_model_ref="llama-3.1-8b@sha256:abc123",
        layer_hashes=[b2_256(layer)],
        data_provenance={"merkle_root": "00" * 32, "corpus_hash": "aa" * 32, "sources": ["corpus-a"]},
        authority={"delegation_ref": "gns://acme/cert/42", "human_principal": "camilo@acme",
                   "trust_tier": "release"},
        conformance={"harness_version": "landing/0.1", "result": "pass",
                     "per_policy": {"artifact_integrity": "pass", "data_provenance": "pass"}},
        permitted_actions=["grafomem_retrieve", "grafomem_compose"],
        layer_bytes=[layer],
    )


def main() -> int:
    if not DB:
        print("ERROR: set GRAFOMEM_DB_URL"); return 2

    key = os.urandom(32)                       # Ed25519 seed for sign_provenance
    svc = LandingService(DB, signing_key=key)  # gateway=None -> ungated baseline
    svc.ensure_schema()

    results: list[tuple[str, bool, str]] = []
    st: dict = {}

    def gate(name, fn):
        try:
            fn(); results.append((name, True, ""))
        except AssertionError as e:
            results.append((name, False, str(e) or "assertion failed"))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))

    # G1 — issuance
    def g1():
        cert = svc.issue_certificate(TENANT, good_request())
        assert cert.get("certificate_id"), "no certificate_id returned"
        st["cid"] = cert["certificate_id"]
    gate("G1  issuance", g1)

    # G2 — persistence round-trip (the bug that bit us: document must survive the DB)
    def g2():
        row = svc.get_certificate(TENANT, st["cid"])
        assert row.get("document"), "no signed document stored"
        assert row["document"]["artifact"]["artifact_ref"].startswith("oci://"), "artifact lost"
    gate("G2  persistence round-trip", g2)

    # G3 — Ed25519 signed
    def g3():
        d = svc.get_certificate(TENANT, st["cid"])["document"]
        assert d.get("signature") and d.get("signer_public_key"), "certificate not signed"
    gate("G3  ed25519-signed", g3)

    # G4 — verification authentic + the five-question reconstruction
    def g4():
        v = svc.verify_certificate(TENANT, st["cid"])
        assert v["passed"], f"authentic cert failed verify: {v['checks']}"
        assert all(v["checks"].values()), f"a check failed: {v['checks']}"
        r = v["reconstruction"]
        for q in ("what", "from_where", "under_whom", "cleared_how", "may_do"):
            assert r.get(q), f"reconstruction missing '{q}'"
    gate("G4  verification authentic + 5-question reconstruction", g4)

    # G5 — tamper-evidence (NON-VACUOUS: mutate a signed field in the DB, verify must fail)
    def g5():
        row = svc.get_certificate(TENANT, st["cid"])
        doc = row["document"]; doc["conformance"]["result"] = "fail"
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("UPDATE landing_certificates SET document=%s WHERE certificate_id=%s",
                        (Jsonb(doc), st["cid"]))
        v = svc.verify_certificate(TENANT, st["cid"])
        assert not v["checks"]["signature"], "tampered document still verified — signature vacuous!"
        assert not v["passed"], "tampered certificate still passed"
    gate("G5  tamper-evidence (non-vacuous)", g5)

    # G6 — provenance must be sealed (no merkle_root -> reject)
    def g6():
        bad = good_request(); bad.data_provenance = {"corpus_hash": "aa" * 32}
        try:
            svc.issue_certificate(TENANT, bad); assert False, "unsealed provenance accepted"
        except LandingError:
            pass
    gate("G6  provenance seal required", g6)

    # G7 — artifact integrity (layer bytes must match declared hashes)
    def g7():
        bad = good_request(); bad.layer_bytes = [b"tampered-weights"]
        try:
            svc.issue_certificate(TENANT, bad); assert False, "artifact hash mismatch accepted"
        except LandingError:
            pass
    gate("G7  artifact integrity required", g7)

    # G8 — governance DENY is enforced
    def g8():
        denied = LandingService(DB, signing_key=key, gateway=_Gateway(False, "denied"))
        try:
            denied.issue_certificate(TENANT, good_request()); assert False, "deny not enforced"
        except LandingDenied:
            pass
    gate("G8  governance deny enforced", g8)

    # G9 — governance HITL: escalate parks, resume issues a verifiable cert
    def g9():
        hitl = LandingService(DB, signing_key=key, gateway=_Gateway(False, "escalated"))
        try:
            hitl.issue_certificate(TENANT, good_request()); assert False, "escalation not parked"
        except LandingPendingHITL as e:
            pending = e.certificate_id
        cert = hitl.resume(TENANT, pending, approved=True, approver="approver@acme")
        v = hitl.verify_certificate(TENANT, cert["certificate_id"])
        assert v["passed"], "resumed (approved) certificate fails verification"
    gate("G9  governance HITL escalate + resume", g9)

    # G10 — deterministic BLAKE2b-128 certificate id
    def g10():
        a = compute_certificate_id("t", "art", "root", "del", "pass", "123.000000")
        b = compute_certificate_id("t", "art", "root", "del", "pass", "123.000000")
        assert a == b, "certificate id not deterministic"
        assert len(a) == 32, f"id not 128-bit hex (len={len(a)})"
        int(a, 16)  # raises if not hex
    gate("G10 deterministic BLAKE2b-128 id", g10)

    # cleanup test rows
    try:
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM landing_certificates WHERE tenant_id=%s", (TENANT,))
    except Exception:
        pass

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n  landing self-conformance  (tenant {TENANT})\n  " + "-" * 52)
    for name, ok, msg in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        -> {msg}" if not ok else ""))
    print("  " + "-" * 52)
    print(f"  {passed}/{len(results)} gates green\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
