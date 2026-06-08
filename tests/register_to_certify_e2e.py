#!/usr/bin/env python3
"""
tests/register_to_certify_e2e.py — R1 -> R3 closed-loop integration.

Registers an artifact (R1), issues a Landing Certificate (R3) for the SAME artifact with the
registry wired in, and asserts the registry row auto-flips to `certified` with the certificate
linked — proving the derived content-addressed id matches across services.

    GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem \
        python3 tests/register_to_certify_e2e.py
"""
from __future__ import annotations
import os, sys, uuid

from aml.cloud.artifact_registry import ArtifactRegistryService, ArtifactRegisterRequest, b2_256
from aml.cloud.landing_service import LandingService, LandingIssueRequest

DB = os.environ.get("GRAFOMEM_DB_URL")
if not DB:
    print("ERROR: set GRAFOMEM_DB_URL"); sys.exit(2)

T = f"e2e-{uuid.uuid4().hex[:8]}"
key = os.urandom(32)

ar = ArtifactRegistryService(DB, signing_identity=_MockId(key)); ar.ensure_schema()
ls = LandingService(DB, signing_identity=_MockId(key), registry=ar); ls.ensure_schema()   # <-- registry wired

l0, l1 = b"adapter-w0", b"adapter-w1"
layers = [{"media_type": "application/vnd.modelpack.lora", "digest": b2_256(l0), "size": len(l0)},
          {"media_type": "application/vnd.modelpack.lora", "digest": b2_256(l1), "size": len(l1)}]
layer_hashes = [l["digest"] for l in layers]
COMMON = dict(artifact_ref="oci://e2e/util-lora:1.0", base_model_ref="llama-3.1-8b@sha256:abc", kind="lora+rag")

ok = False
try:
    # 1. register (R1)
    rec = ar.register(T, ArtifactRegisterRequest(layers=layers, metadata={"by": "e2e"},
                                                 layer_bytes=[l0, l1], **COMMON))
    assert rec["status"] == "registered", f"unexpected status {rec['status']}"

    # 2. issue landing certificate (R3) for the SAME artifact — registry link is derived
    cert = ls.issue_certificate(T, LandingIssueRequest(
        layer_hashes=layer_hashes,
        data_provenance={"merkle_root": "00" * 32, "corpus_hash": "aa" * 32},
        authority={"delegation_ref": "gns://acme/cert/1", "human_principal": "camilo@acme", "trust_tier": "release"},
        conformance={"harness_version": "landing/0.1", "result": "pass", "per_policy": {}},
        permitted_actions=["grafomem_retrieve"], layer_bytes=[l0, l1], **COMMON))

    # 3. the registry row should now be certified and linked to the cert
    linked = ar.get(T, rec["artifact_id"])
    ok = (linked["status"] == "certified" and linked["certificate_id"] == cert["certificate_id"])

    print(f"  register  -> artifact_id {rec['artifact_id'][:16]}  (registered)")
    print(f"  issue     -> certificate {cert['certificate_id'][:16]}")
    print(f"  registry  -> status={linked['status']}  linked={(linked['certificate_id'] or '')[:16]}")
    print(f"\n  R1->R3 closed loop: {'PASS' if ok else 'FAIL'}\n")
finally:
    for svc, tbl in [(ar, "artifact_registry"), (ls, "landing_certificates")]:
        try:
            with svc._get_conn() as c, c.cursor() as cur:
                cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (T,))
        except Exception:
            pass

sys.exit(0 if ok else 1)


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
