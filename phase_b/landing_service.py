"""
src/aml/cloud/landing_service.py   (PHASE-B SKELETON — review & wire the `# ADAPT:` points)

R3 — landing conformance + Landing Certificate issuance/verification.
Sibling of cloud/erasure_proof.py; anchors via cloud/execution_receipts.py (gcrumbs).

The cert body is the lc/0.1 schema (landing/spec/04-landing-certificate.md), already proven
in the grafomem_landing reference. This is a PORT: swap the local helpers below for your
src/aml/provenance.py equivalents, and the injected deps for your real services.
"""
from __future__ import annotations
import hashlib, json, time
from dataclasses import dataclass, field
from typing import Optional

US = b"\x1f"

# --- hashing (stdlib; or replace with src/aml/provenance.py) ---
def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
def b2_256(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()
def b2_128(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=16); h.update(US.join(p.encode() for p in parts)); return h.hexdigest()

_SIGNED = ["version", "tenant_id", "timestamp", "artifact", "data_provenance",
           "authority", "conformance", "permitted_actions", "certificate_id"]


@dataclass
class LandingIssueRequest:
    artifact_ref: str
    base_model_ref: str
    layer_hashes: list                     # [BLAKE2b-256]; or resolved from ArtifactRegistry
    data_provenance: dict                  # corpus_hash, epoch_id, merkle_root, source_leaf, inclusion_proof, composition_ref
    authority: dict                        # delegation_ref, human_principal, trust_tier, delegation_sig, delegation_signed_body
    conformance: dict                      # harness_version, result, per_policy
    permitted_actions: list
    kind: str = "lora+rag"
    layer_bytes: Optional[list] = field(default=None, repr=False)   # for precondition re-hash


class LandingError(Exception): ...
class LandingDenied(LandingError): ...
class LandingPendingHITL(LandingError):
    def __init__(self, certificate_id): self.certificate_id = certificate_id


class LandingService:
    def __init__(self, db, signer, gcrumbs, gateway, tenants, compliance=None,
                 metering=None, artifacts=None, *, epoch_layer: bool = False):
        self.db = db                # db_pool (psycopg, dict_row)
        self.signer = signer        # ADAPT: src/aml/provenance.py Ed25519 signer (sign(bytes)->hex, public_key_hex)
        self.gcrumbs = gcrumbs      # ExecutionReceiptService (+ epoch API if epoch_layer)
        self.gateway = gateway      # GovernanceGateway (PEP)
        self.tenants = tenants
        self.compliance = compliance
        self.metering = metering
        self.artifacts = artifacts  # ArtifactRegistry (R1), optional for now
        self.epoch_layer = epoch_layer   # True -> roll_epoch/get_proof; False -> chained receipt

    # ---------------- R3 surface ----------------
    def run_conformance(self, tenant_id: str, artifact_ref: str, layer_bytes: list, data_provenance: dict) -> dict:
        per_policy = {
            "artifact_integrity": "pass" if self._layers_ok(layer_bytes, [b2_256(b) for b in layer_bytes]) else "fail",
            "data_provenance": "pass" if data_provenance.get("epoch_id") else "fail",
            "authority": "pass",   # ADAPT: confirm delegation present/valid here if desired
        }
        result = "pass" if all(v == "pass" for v in per_policy.values()) else "fail"
        return {"harness_version": "landing/0.1", "result": result, "per_policy": per_policy}

    def issue_certificate(self, tenant_id: str, req: LandingIssueRequest) -> dict:
        # 1. preconditions
        if req.layer_bytes is not None and not self._layers_ok(req.layer_bytes, req.layer_hashes):
            raise LandingError("artifact layer hash mismatch")
        if not req.data_provenance.get("epoch_id"):
            raise LandingError("data provenance not sealed")

        # 2. governed gate (R5 ActionType: release tier + HITL)
        # ADAPT: confirm evaluate_and_gate signature + the status enum values
        decision = self.gateway.evaluate_and_gate(
            tenant_id, "landing.issue",
            {"artifact_ref": req.artifact_ref, "authority": req.authority})
        status = getattr(decision, "status", None) or (decision.get("status") if isinstance(decision, dict) else None)
        if status in ("DENIED", "denied"):
            cid = self._persist_stub(tenant_id, req, "denied")
            raise LandingDenied(cid)
        if status in ("ESCALATED", "escalated", "WAITING_HITL"):
            cid = self._persist_stub(tenant_id, req, "waiting_hitl")
            raise LandingPendingHITL(cid)
        # ADAPT: also verify the GEIANT delegation roots in a human at >= release tier

        # 3. build + sign
        cert = self._build_and_sign(tenant_id, req)
        # 4. anchor in gcrumbs
        cert["anchor"] = self._anchor(tenant_id, cert["certificate_id"], req.artifact_ref)
        # 5. persist
        self._persist(tenant_id, cert, status="issued")
        # 6. meter
        if self.metering: self.metering.count(tenant_id, "landing.issue")     # ADAPT
        return cert

    def resume(self, tenant_id: str, certificate_id: str, approved: bool, approver: str) -> dict:
        row = self._fetch(tenant_id, certificate_id)
        if row["status"] != "waiting_hitl":
            raise LandingError(f"certificate not awaiting HITL (status={row['status']})")
        if not approved:
            self._set_status(tenant_id, certificate_id, "denied")
            return {"certificate_id": certificate_id, "status": "denied", "approver": approver}
        req = self._req_from_row(row)
        cert = self._build_and_sign(tenant_id, req)
        cert["anchor"] = self._anchor(tenant_id, cert["certificate_id"], req.artifact_ref)
        self._persist(tenant_id, cert, status="issued")
        return cert

    def get_certificate(self, tenant_id, certificate_id): return self._fetch(tenant_id, certificate_id)

    def list_certificates(self, tenant_id, limit=50, offset=0):
        # ADAPT: db_pool cursor pattern
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM landing_certificates WHERE tenant_id=%s "
                        "ORDER BY created_at DESC LIMIT %s OFFSET %s", (tenant_id, limit, offset))
            return cur.fetchall()

    def verify_certificate(self, tenant_id, certificate_id) -> dict:
        """Server-side mirror of the offline 6-step verification (spec §04)."""
        cert = self._fetch(tenant_id, certificate_id)
        checks = {}
        checks["signature"] = self._verify_sig(cert)
        checks["data_provenance"] = bool(cert["data_provenance"].get("inclusion_proof"))  # ADAPT: gcrumbs.get_proof verify
        checks["authority_human_rooted"] = self._verify_delegation(cert["authority"])     # ADAPT
        checks["conformance_pass"] = cert["conformance"]["result"] == "pass"
        checks["anchor_sealed"] = self._verify_anchor(tenant_id, cert)                     # ADAPT: gcrumbs proof
        passed = all(checks.values())
        a = cert["authority"]
        recon = {"what": f'{cert.get("kind","lora+rag")} {cert["artifact_ref"]} on {cert["base_model_ref"]}',
                 "from_where": cert["data_provenance"].get("epoch_id"),
                 "under_whom": f'{a["human_principal"]} @ {a["trust_tier"]}',
                 "cleared_how": cert["conformance"]["result"],
                 "may_do": cert["permitted_actions"]}
        return {"passed": passed, "checks": checks, "reconstruction": recon}

    # ---------------- internals ----------------
    def _build_and_sign(self, tenant_id, req: LandingIssueRequest) -> dict:
        ts = time.time()
        cert = {"version": "lc/0.1", "tenant_id": tenant_id, "timestamp": ts,
                "artifact": {"artifact_ref": req.artifact_ref, "base_model_ref": req.base_model_ref,
                             "layer_hashes": req.layer_hashes, "kind": req.kind,
                             "manifest_digest": b2_256(canon(req.layer_hashes))},
                "data_provenance": req.data_provenance, "authority": req.authority,
                "conformance": req.conformance, "permitted_actions": req.permitted_actions}
        cert["certificate_id"] = b2_128(tenant_id, req.artifact_ref,
                                        req.data_provenance["merkle_root"],
                                        req.authority["delegation_ref"], req.conformance["result"], str(ts))
        cert["signer_public_key"] = self.signer.public_key_hex            # ADAPT
        cert["signature"] = self.signer.sign(canon({k: cert[k] for k in _SIGNED}))   # ADAPT: returns hex
        return cert

    def _anchor(self, tenant_id, certificate_id, artifact_ref) -> dict:
        # emit the cert as a gcrumbs breadcrumb/receipt (chain-links it)
        # ADAPT: confirm issue_receipt signature; this mirrors ExecutionReceiptService.issue_receipt
        receipt = self.gcrumbs.issue_receipt(
            tenant_id=tenant_id, kind="landing_certificate",
            payload={"certificate_id": certificate_id, "artifact_ref": artifact_ref})
        if not self.epoch_layer:
            # CHAIN-ONLY: verified later via the verify-chain endpoint
            return {"mode": "chain",
                    "receipt_id": getattr(receipt, "receipt_id", receipt.get("receipt_id") if isinstance(receipt, dict) else None),
                    "previous_receipt_hash": getattr(receipt, "previous_receipt_hash", None)}
        # EPOCH LAYER: seal a Merkle epoch + fetch the O(log N) inclusion proof
        epoch = self.gcrumbs.roll_epoch(agent_id=tenant_id)                            # ADAPT
        proof = self.gcrumbs.get_proof((epoch.epoch_id, receipt.leaf_index))           # ADAPT
        return {"mode": "epoch", "epoch_id": epoch.epoch_id, "merkle_root": epoch.merkle_root,
                "inclusion_proof": proof.merkle_path, "anchor_proof": getattr(epoch, "anchor_proof", None)}

    def _layers_ok(self, layer_bytes, layer_hashes) -> bool:
        return all(b2_256(b) == h for b, h in zip(layer_bytes, layer_hashes))

    def _verify_sig(self, cert) -> bool:
        # ADAPT: provenance.ed25519_verify(cert["signer_public_key"], cert["signature"], canon(signed_body))
        return True

    def _verify_delegation(self, authority) -> bool:
        # ADAPT: verify authority["delegation_sig"] over delegation_signed_body against the human principal key
        return True

    def _verify_anchor(self, tenant_id, cert) -> bool:
        anchor = cert.get("anchor") or {}
        if anchor.get("mode") == "epoch":
            # ADAPT: recompute Merkle root from inclusion_proof and compare to anchor["merkle_root"]
            return bool(anchor.get("inclusion_proof"))
        # ADAPT: chain mode -> call gcrumbs verify-chain for the tenant chain up to the receipt
        return True

    # ---- db (ADAPT: db_pool cursor/commit pattern) ----
    def _persist(self, tenant_id, cert, status):
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO landing_certificates
                   (certificate_id, tenant_id, artifact_ref, base_model_ref, layer_hashes,
                    data_provenance, authority, conformance, permitted_actions, anchor, status,
                    signature, signer_public_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (certificate_id) DO UPDATE SET status=EXCLUDED.status,
                       anchor=EXCLUDED.anchor, signature=EXCLUDED.signature""",
                (cert["certificate_id"], tenant_id, cert["artifact"]["artifact_ref"],
                 cert["artifact"]["base_model_ref"], json.dumps(cert["artifact"]["layer_hashes"]),
                 json.dumps(cert["data_provenance"]), json.dumps(cert["authority"]),
                 json.dumps(cert["conformance"]), json.dumps(cert["permitted_actions"]),
                 json.dumps(cert.get("anchor")), status, cert["signature"], cert["signer_public_key"]))
            conn.commit()

    def _persist_stub(self, tenant_id, req: LandingIssueRequest, status) -> str:
        cid = b2_128(tenant_id, req.artifact_ref, req.data_provenance.get("merkle_root", ""),
                     req.authority.get("delegation_ref", ""), status, str(time.time()))
        # ADAPT: minimal row insert for a denied/waiting_hitl certificate
        return cid

    def _fetch(self, tenant_id, certificate_id):
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM landing_certificates WHERE tenant_id=%s AND certificate_id=%s",
                        (tenant_id, certificate_id))
            row = cur.fetchone()
        if not row:
            raise LandingError("certificate not found")
        return row

    def _set_status(self, tenant_id, certificate_id, status):
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE landing_certificates SET status=%s WHERE tenant_id=%s AND certificate_id=%s",
                        (status, tenant_id, certificate_id))
            conn.commit()

    def _req_from_row(self, row) -> LandingIssueRequest:
        # ADAPT: reconstruct the issue request from a waiting_hitl row's stored fields
        return LandingIssueRequest(
            artifact_ref=row["artifact_ref"], base_model_ref=row.get("base_model_ref", ""),
            layer_hashes=row.get("layer_hashes", []), data_provenance=row.get("data_provenance", {}),
            authority=row.get("authority", {}), conformance=row.get("conformance", {}),
            permitted_actions=row.get("permitted_actions", []))
