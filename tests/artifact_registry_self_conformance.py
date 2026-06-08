#!/usr/bin/env python3
"""
tests/artifact_registry_self_conformance.py — two-sided suite for the Artifact Registry (R1).

In-process against the live service + your real Postgres (GRAFOMEM_DB_URL). Each gate is
non-vacuous: the registry both ACCEPTS valid artifacts and REJECTS tampered / mismatched /
unauthorized ones, and the content-addressed id is proven deterministic.

    GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem \
        python tests/artifact_registry_self_conformance.py
"""
from __future__ import annotations
import os, sys, uuid

from aml.cloud.artifact_registry import (
    ArtifactRegistryService, ArtifactRegisterRequest, RegistryError, RegistryDenied,
    RegistryPendingHITL, compute_artifact_id, compute_manifest_digest, b2_256,
)
from psycopg.types.json import Jsonb

DB = os.environ.get("GRAFOMEM_DB_URL")
TENANT = f"r1conf-{uuid.uuid4().hex[:8]}"


class _Log:
    def __init__(self, result): self.result = result

class _Gateway:
    def __init__(self, allowed, result=None): self._a, self._r = allowed, result
    def evaluate_and_gate(self, tenant, operation, context):
        return (self._a, [] if self._a else [_Log(self._r)])


def good_request() -> ArtifactRegisterRequest:
    l0, l1 = b"lora-A-weights", b"lora-B-weights"
    return ArtifactRegisterRequest(
        artifact_ref="oci://registry/acme/util-lora:1.0",
        base_model_ref="llama-3.1-8b@sha256:abc123",
        layers=[{"media_type": "application/vnd.modelpack.lora", "digest": b2_256(l0), "size": len(l0)},
                {"media_type": "application/vnd.modelpack.lora", "digest": b2_256(l1), "size": len(l1)}],
        kind="lora+rag",
        metadata={"author": "camilo@acme", "framework": "peft"},
        layer_bytes=[l0, l1],
    )


def main() -> int:
    if not DB:
        print("ERROR: set GRAFOMEM_DB_URL"); return 2

    key = os.urandom(32)
    svc = ArtifactRegistryService(DB, signing_identity=_MockId(key))
    svc.ensure_schema()

    results, st = [], {}

    def gate(name, fn):
        try:
            fn(); results.append((name, True, ""))
        except AssertionError as e:
            results.append((name, False, str(e) or "assertion failed"))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))

    def a1():
        rec = svc.register(TENANT, good_request())
        assert rec.get("artifact_id"), "no artifact_id"
        st["aid"] = rec["artifact_id"]
    gate("A1  registration", a1)

    def a2():
        row = svc.get(TENANT, st["aid"])
        assert row.get("document"), "no signed document stored"
        assert row["document"]["manifest_digest"], "no manifest digest"
    gate("A2  persistence round-trip", a2)

    def a3():
        d = svc.get(TENANT, st["aid"])["document"]
        assert d.get("signature") and d.get("signer_public_key"), "receipt not signed"
    gate("A3  ed25519-signed receipt", a3)

    def a4():
        v = svc.verify(TENANT, st["aid"])
        assert v["passed"], f"authentic receipt failed verify: {v['checks']}"
        assert v["checks"]["signature"] and v["checks"]["manifest_consistent"], v["checks"]
    gate("A4  verification authentic (sig + manifest consistent)", a4)

    def a5():
        row = svc.get(TENANT, st["aid"]); doc = row["document"]
        doc["layer_hashes"] = ["00" * 32]                     # tamper a signed field
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("UPDATE artifact_registry SET document=%s WHERE artifact_id=%s",
                        (Jsonb(doc), st["aid"]))
        v = svc.verify(TENANT, st["aid"])
        assert not v["checks"]["signature"], "tampered receipt still verified — signature vacuous!"
        assert not v["passed"], "tampered receipt still passed"
    gate("A5  tamper-evidence (non-vacuous)", a5)

    def a6():
        # content-addressed: re-registering identical content yields the SAME id (idempotent)
        r1 = svc.register(TENANT, good_request())
        r2 = svc.register(TENANT, good_request())
        assert r1["artifact_id"] == r2["artifact_id"], "id not content-addressed / not idempotent"
        man = compute_manifest_digest("m", "lora", ["h1", "h2"])
        aid = compute_artifact_id("t", "ref", "m", man)
        assert len(aid) == 32, f"artifact_id not BLAKE2b-128 (len={len(aid)})"
        assert len(man) == 64, f"manifest_digest not BLAKE2b-256 (len={len(man)})"
    gate("A6  content-addressed deterministic id + digest", a6)

    def a7():
        # integrity check is non-vacuous: correct hashes match, wrong hashes do not.
        # distinct artifact_ref -> a fresh (untampered) registration, not the A5-tampered one
        # (registration is content-addressed/idempotent, per A6).
        req = good_request(); req.artifact_ref = "oci://registry/acme/integrity:1.0"
        rec = svc.register(TENANT, req)
        real = rec["layer_hashes"]
        assert svc.check_integrity(TENANT, rec["artifact_id"], real)["match"] is True, "matching bytes rejected"
        assert svc.check_integrity(TENANT, rec["artifact_id"], ["ff" * 32])["match"] is False, "mismatch accepted"
    gate("A7  integrity check (non-vacuous)", a7)

    def a8():
        denied = ArtifactRegistryService(DB, signing_identity=_MockId(key), gateway=_Gateway(False, "denied"))
        req = good_request(); req.artifact_ref = "oci://registry/acme/denied:1.0"  # distinct id so not idempotent-hit
        try:
            denied.register(TENANT, req); assert False, "deny not enforced"
        except RegistryDenied:
            pass
    gate("A8  governance deny enforced", a8)

    def a9():
        hitl = ArtifactRegistryService(DB, signing_identity=_MockId(key), gateway=_Gateway(False, "escalated"))
        req = good_request(); req.artifact_ref = "oci://registry/acme/hitl:1.0"
        try:
            hitl.register(TENANT, req); assert False, "escalation not parked"
        except RegistryPendingHITL as e:
            pending = e.artifact_id
        rec = hitl.resume(TENANT, pending, approved=True, approver="approver@acme")
        v = hitl.verify(TENANT, rec["artifact_id"])
        assert v["passed"], "resumed (approved) artifact fails verification"
    gate("A9  governance HITL escalate + resume", a9)

    def a10():
        req = good_request(); req.artifact_ref = "oci://registry/acme/tobecertified:1.0"
        rec = svc.register(TENANT, req)
        linked = svc.certify(TENANT, rec["artifact_id"], "landing-cert-deadbeef")
        assert linked["status"] == "certified", f"status not certified: {linked['status']}"
        assert linked["certificate_id"] == "landing-cert-deadbeef", "certificate_id not linked"
    gate("A10 certify-link to R3", a10)

    try:
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM artifact_registry WHERE tenant_id=%s", (TENANT,))
    except Exception:
        pass

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n  artifact-registry self-conformance  (tenant {TENANT})\n  " + "-" * 56)
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
