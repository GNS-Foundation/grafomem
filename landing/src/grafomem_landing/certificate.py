"""R3 / B1 — the Landing Certificate: issue + fully OFFLINE verification.

A signed, gcrumbs-anchored sibling of the Erasure Certificate and Conformance Report.
Phase B: becomes src/aml/cloud/landing_service.py (+ landing_routes.py), reusing the
signing/cert pattern of cloud/erasure_proof.py and cloud/regulatory.py.
"""
import time
from .hashing import canon, b2_128, b2_256
from .identity import pub_hex, sign_hex, verify_hex
from .crumbs import Crumbs

_SIGNED = ["version", "tenant_id", "timestamp", "artifact", "data_provenance",
           "authority", "conformance", "permitted_actions", "certificate_id"]


def issue_landing_certificate(tenant, artifact, data_provenance, authority, conformance,
                              permitted_actions, signer_priv):
    ts = time.time()
    cert = {"version": "lc/0.1", "tenant_id": tenant, "timestamp": ts,
            "artifact": artifact, "data_provenance": data_provenance,
            "authority": authority, "conformance": conformance,
            "permitted_actions": permitted_actions}
    cert["certificate_id"] = b2_128(tenant, artifact["artifact_ref"], data_provenance["merkle_root"],
                                    authority["delegation_ref"], conformance["result"], str(ts))
    cert["signer_public_key"] = pub_hex(signer_priv)
    cert["signature"] = sign_hex(signer_priv, canon({k: cert[k] for k in _SIGNED}))
    return cert


def verify_landing_certificate(cert, artifact_layer_bytes, human_pubkey_lookup,
                               anchor_root, anchor_proof, cert_leaf):
    """Verify with ONLY {cert + chain + keys + artifact bytes} — no live service.
    Returns (ok, reconstruction, checks)."""
    checks = {}
    checks["signature"] = verify_hex(cert["signer_public_key"], cert["signature"],
                                     canon({k: cert[k] for k in _SIGNED}))
    checks["artifact_layers"] = all(
        b2_256(artifact_layer_bytes[i]) == h for i, h in enumerate(cert["artifact"]["layer_hashes"]))
    dp = cert["data_provenance"]
    checks["data_provenance"] = Crumbs.verify_inclusion(dp["source_leaf"], dp["inclusion_proof"], dp["merkle_root"])
    a = cert["authority"]
    checks["authority_human_rooted"] = verify_hex(
        human_pubkey_lookup(a["human_principal"]), a["delegation_sig"], a["delegation_signed_body"].encode())
    checks["conformance_pass"] = cert["conformance"]["result"] == "pass"
    checks["anchor_sealed"] = Crumbs.verify_inclusion(cert_leaf, anchor_proof, anchor_root)
    ok = all(checks.values())
    reconstruction = {
        "what": f'{cert["artifact"]["kind"]} artifact {cert["artifact"]["artifact_ref"]} on base {cert["artifact"]["base_model_ref"]}',
        "from_where": f'corpus {cert["data_provenance"]["corpus_hash"][:16]}… sealed in epoch {cert["data_provenance"]["epoch_id"]}',
        "under_whom": f'{a["human_principal"]} at tier {a["trust_tier"]}',
        "cleared_how": f'landing conformance {cert["conformance"]["result"]} (harness {cert["conformance"]["harness_version"]})',
        "may_do": cert["permitted_actions"],
    }
    return ok, reconstruction, checks
