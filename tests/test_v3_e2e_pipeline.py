#!/usr/bin/env python3
"""
tests/test_v3_e2e_pipeline.py — Five-stage end-to-end pipeline test.

Exercises R2 → R1 → R3 → R4 → R5 as a single governed flow, proving
every cross-link and the full pipeline mechanically.

    GRAFOMEM_DB_URL=postgresql://... pytest tests/test_v3_e2e_pipeline.py -v
    GRAFOMEM_DB_URL=postgresql://...  python3 tests/test_v3_e2e_pipeline.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import time

DB = os.environ.get("GRAFOMEM_DB_URL")

# ---------------------------------------------------------------------------
# Stub gateway — always allows
# ---------------------------------------------------------------------------

class _Log:
    def __init__(self, d, r): self.decision = d; self.reason = r

class _AllowGateway:
    def evaluate_and_gate(self, tenant, operation, context):
        return (True, [_Log("allow", "e2e auto-allow")])


def b2(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def main() -> int:
    if not DB:
        print("SKIP: set GRAFOMEM_DB_URL"); return 0

    # Lazy imports so module loads even without deps
    from aml.cloud.provenance_customs import ProvenanceCustomsService, CorpusRegisterRequest
    from aml.cloud.artifact_registry import ArtifactRegistryService, ArtifactRegisterRequest
    from aml.cloud.landing_service import LandingService, LandingIssueRequest
    from aml.cloud.composition_governance import CompositionGovernanceService, ComposeRequest
    from aml.cloud.world_model import WorldModelService, ActionInvocation

    key = os.urandom(32)
    gw = _AllowGateway()
    tenant = f"e2e-{int(time.time())}"

    ok = 0
    total = 6

    def step(name, fn):
        nonlocal ok
        try:
            fn()
            print(f"  ✅ {name}")
            ok += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            import traceback; traceback.print_exc()

    print("\n═══ GRAFOMEM v3.0 — Five-Stage E2E Pipeline ═══\n")

    # Shared state between stages
    state = {}

    # --- R2: Seal a corpus ---
    def stage_r2():
        pc = ProvenanceCustomsService(DB, signing_key=key, gateway=gw)
        pc.ensure_schema()
        corpus = pc.register_corpus(tenant, CorpusRegisterRequest(
            name="e2e-pipeline-corpus",
            sources=[
                {"id": "module-a.py", "license": "MIT", "content_hash": b2(b"module-a"), "record_count": 100},
                {"id": "module-b.py", "license": "MIT", "content_hash": b2(b"module-b"), "record_count": 200},
                {"id": "config.yaml", "license": "MIT", "content_hash": b2(b"config"), "record_count": 30},
            ],
            attestations={"representativeness": "e2e test data", "bias_examination": "not applicable — code"},
        ))
        assert corpus["clearance"] == "cleared", f"expected cleared, got {corpus['clearance']}"
        block = pc.provenance_block(tenant, corpus["corpus_id"])
        assert block["merkle_root"], "provenance block missing merkle_root"
        # Verify inclusion proof
        proof = pc.inclusion_proof(tenant, corpus["corpus_id"], "module-a.py")
        assert proof.get("valid") or proof.get("proof"), "inclusion proof failed"
        state["corpus"] = corpus
        state["provenance_block"] = block
        state["pc"] = pc

    step("R2  Seal corpus + Merkle proof + provenance block", stage_r2)

    # --- R1: Register artifact ---
    def stage_r1():
        ar = ArtifactRegistryService(DB, signing_key=key, gateway=gw)
        ar.ensure_schema()
        layer_data = b"e2e-test-layer-content"
        layer_hash = b2(layer_data)
        artifact = ar.register(tenant, ArtifactRegisterRequest(
            artifact_ref="oci://gns/e2e-pipeline:v1",
            base_model_ref="gemini-2.5-pro",
            layers=[{"media_type": "application/x-python", "digest": layer_hash, "size": len(layer_data)}],
            kind="rag-kb",
            metadata={"corpus_id": state["corpus"]["corpus_id"]},
        ))
        assert artifact.get("artifact_id"), "artifact_id missing"
        # Verify receipt
        v = ar.verify(tenant, artifact["artifact_id"])
        assert v.get("passed") or v.get("valid") or v.get("signature_valid"), f"artifact verify failed: {v}"
        state["artifact"] = artifact
        state["ar"] = ar
        state["layer_hash"] = layer_hash

    step("R1  Register artifact + verify receipt", stage_r1)

    # --- R3: Issue landing certificate ---
    def stage_r3():
        ls = LandingService(DB, signing_key=key, gateway=gw, epoch_anchor=False)
        ls.ensure_schema()
        ls.registry = state["ar"]  # wire auto-certify
        cert = ls.issue_certificate(tenant, LandingIssueRequest(
            artifact_ref="oci://gns/e2e-pipeline:v1",
            base_model_ref="gemini-2.5-pro",
            layer_hashes=[state["layer_hash"]],
            data_provenance=state["provenance_block"],
            authority={"delegation_ref": "e2e-root", "human_principal": "e2e@test", "trust_tier": "release"},
            conformance={"suite": "e2e-pipeline", "result": "pass", "gates_passed": 50, "gates_total": 50,
                          "per_policy": {"artifact_integrity": "pass", "data_provenance": "pass", "authority": "pass"}},
            permitted_actions=["read_fact", "write_fact", "evaluate_governance"],
            kind="rag-kb",
        ))
        assert cert.get("certificate_id"), "certificate_id missing"
        # Verify certificate (non-vacuous)
        v = ls.verify_certificate(tenant, cert["certificate_id"])
        assert v.get("valid") or v.get("passed"), f"certificate verify failed: {v}"
        state["cert"] = cert
        state["ls"] = ls

    step("R3  Issue landing certificate + verify (non-vacuous)", stage_r3)

    # --- Cross-link: R3→R1 auto-certify ---
    def stage_crosslink():
        ar = state["ar"]
        refreshed = ar.get(tenant, state["artifact"]["artifact_id"])
        cert_id = refreshed.get("certificate_id")
        assert cert_id == state["cert"]["certificate_id"], \
            f"R3→R1 auto-certify failed: expected {state['cert']['certificate_id']}, got {cert_id}"

    step("R3→R1 auto-certify cross-link verified", stage_crosslink)

    # --- R4: Compose ---
    def stage_r4():
        cg = CompositionGovernanceService(DB, signing_key=key, gateway=gw)
        cg.ensure_schema()
        comp = cg.compose(tenant, ComposeRequest(
            composition_kind="rag-kb+base",
            members=[
                {"ref_id": state["artifact"]["artifact_id"], "license": "MIT", "certified": True},
                {"ref_id": "gemini-2.5-pro", "license": "proprietary", "certified": True},
            ],
            target_ref="oci://gns/e2e-composed:v1",
            authority={"trust_tier": "release"},
            required_trust_tier="verified",
        ))
        assert comp.get("composition_id"), "composition_id missing"
        # Verify composition receipt
        v = cg.verify(tenant, comp["composition_id"])
        assert v.get("passed"), f"composition verify failed: {v}"
        # Get composed artifact descriptor
        desc = cg.composed_artifact(tenant, comp["composition_id"])
        assert desc, "composed_artifact returned nothing"
        state["comp"] = comp
        state["comp_desc"] = desc

    step("R4  Compose + verify receipt + composed_artifact()", stage_r4)

    # --- R5: World-model types + governed action ---
    def stage_r5():
        wm = WorldModelService(DB, signing_key=key, gateway=gw)
        wm.ensure_schema()
        # Object type
        obj_t = wm.register_type(tenant, "object", "E2EPipelineEntity", {
            "properties": {"name": {"type": "string", "required": True}, "version": {"type": "string"}}
        })
        assert obj_t.get("type_id"), "object type_id missing"
        # Action type
        act_t = wm.register_type(tenant, "action", "e2e_deploy", {
            "subject_type": "E2EPipelineEntity", "required_trust_tier": "basic",
            "operation": "worldmodel.action.e2e_deploy",
        })
        assert act_t.get("type_id"), "action type_id missing"
        # Invoke governed action
        receipt = wm.invoke_action(tenant, ActionInvocation(
            action_name="e2e_deploy",
            subject_refs=["entity-1"],
            params={"target": "production"},
            authority={"trust_tier": "verified", "human_principal": "e2e@test"},
        ))
        assert receipt.get("action_id"), "action_id missing"
        # Verify action receipt
        v = wm.verify_action(tenant, receipt["action_id"])
        assert v.get("valid") or v.get("passed") or v.get("signature_valid"), f"action verify failed: {v}"
        state["wm_receipt"] = receipt

    step("R5  Register types + invoke governed action + verify", stage_r5)

    # --- Summary ---
    print(f"\n═══ Pipeline result: {ok}/{total} stages passed ═══\n")
    if ok == total:
        print("  ✅ R2→R1→R3→R4→R5 pipeline COMPLETE")
        print(f"     Corpus:      {state.get('corpus', {}).get('corpus_id', '?')}")
        print(f"     Artifact:    {state.get('artifact', {}).get('artifact_id', '?')}")
        print(f"     Certificate: {state.get('cert', {}).get('certificate_id', '?')}")
        print(f"     Composition: {state.get('comp', {}).get('composition_id', '?')}")
        print(f"     Action:      {state.get('wm_receipt', {}).get('action_id', '?')}")
        print()
    return 0 if ok == total else 1


# ---------------------------------------------------------------------------
# pytest integration
# ---------------------------------------------------------------------------
try:
    import pytest
    pytestmark = pytest.mark.skipif(not DB, reason="set GRAFOMEM_DB_URL")

    def test_v3_e2e_pipeline():
        assert main() == 0, "e2e pipeline failed"
except ImportError:
    pass

if __name__ == "__main__":
    sys.exit(main())
