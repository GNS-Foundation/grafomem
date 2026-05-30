"""
src/aml/cloud/landing_service.py   (PHASE-B — WIRED to your real signatures)

R3 — landing conformance + Landing Certificate issuance/verification.
A near-clone of cloud/erasure_proof.py: BLAKE2b-128 cert id, Ed25519 sign via
provenance.sign_provenance, verify on demand. Gated through GovernanceGateway.

gcrumbs note: your execution_receipts is chain-only + step-oriented, and your erasure
certs are sign-only (not chained). So v1 here is sign-only too. The Merkle-epoch *anchor*
(inclusion proofs) is deferred behind `epoch_anchor` until roll_epoch/get_proof exist
(the CDP Sprint-1 extension) — flip it on then; nothing else changes.
"""
from __future__ import annotations
import hashlib, json, time
from dataclasses import dataclass, field
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

US = b"\x1f"

def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()

def b2_256(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()

def compute_certificate_id(tenant_id: str, artifact_ref: str, provenance_root: str,
                           delegation_ref: str, result: str, ts: float) -> str:
    """BLAKE2b-128 — matches the certificate-id convention in erasure_proof.py / provenance.py."""
    h = hashlib.blake2b(digest_size=16)
    h.update(US.join(s.encode() for s in (tenant_id, artifact_ref, provenance_root, delegation_ref, result, str(ts))))
    return h.hexdigest()

# the fields covered by the signature (the certificate digest)
_SIGNED = ["version", "tenant_id", "timestamp", "artifact", "data_provenance",
           "authority", "conformance", "permitted_actions", "certificate_id"]

def compute_certificate_digest(cert: dict) -> bytes:
    return hashlib.blake2b(canon({k: cert[k] for k in _SIGNED}), digest_size=32).digest()


def _result_value(log) -> str:
    r = getattr(log, "result", log)
    return getattr(r, "value", r)


@dataclass
class LandingIssueRequest:
    artifact_ref: str
    base_model_ref: str
    layer_hashes: list
    data_provenance: dict
    authority: dict
    conformance: dict
    permitted_actions: list
    kind: str = "lora+rag"
    layer_bytes: Optional[list] = field(default=None, repr=False)


class LandingError(Exception): ...
class LandingDenied(LandingError): ...
class LandingPendingHITL(LandingError):
    def __init__(self, certificate_id): self.certificate_id = certificate_id; super().__init__(certificate_id)


class LandingService:
    """Mirrors ErasureProofService(db_url, ..., signing_key=...)."""

    def __init__(self, db_url: str, *, signing_key: Optional[bytes] = None,
                 gateway=None, decision_trail=None, epoch_anchor: bool = False):
        self.db_url = db_url
        self.signing_key = signing_key          # 32-byte Ed25519 seed (same family as erasure_key)
        self.gateway = gateway                  # GovernanceGateway
        self.decision_trail = decision_trail
        self.epoch_anchor = epoch_anchor        # future: gcrumbs Merkle-epoch inclusion proof

    # ---- db (mirrors erasure_proof._get_conn) ----
    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.db_url, row_factory=dict_row, autocommit=True)

    def ensure_schema(self) -> None:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS landing_certificates (
              certificate_id    TEXT PRIMARY KEY,
              tenant_id         TEXT NOT NULL,
              artifact_ref      TEXT NOT NULL,
              base_model_ref    TEXT NOT NULL,
              layer_hashes      JSONB NOT NULL,
              data_provenance   JSONB NOT NULL,
              authority         JSONB NOT NULL,
              conformance       JSONB NOT NULL,
              permitted_actions JSONB NOT NULL,
              anchor            JSONB,
              status            TEXT NOT NULL DEFAULT 'issued',
              signature         TEXT,
              signer_public_key TEXT,
              created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS ix_landing_certs_tenant   ON landing_certificates(tenant_id);
            CREATE INDEX IF NOT EXISTS ix_landing_certs_artifact ON landing_certificates(tenant_id, artifact_ref);
            """)

    # ---- R3 surface ----
    def run_conformance(self, tenant_id: str, artifact_ref: str, layer_bytes: list, data_provenance: dict) -> dict:
        per_policy = {
            "artifact_integrity": "pass" if (layer_bytes and self._layers_ok(layer_bytes, [b2_256(b) for b in layer_bytes])) else ("pass" if not layer_bytes else "fail"),
            "data_provenance": "pass" if data_provenance.get("merkle_root") else "fail",
            "authority": "pass",
        }
        result = "pass" if all(v == "pass" for v in per_policy.values()) else "fail"
        return {"harness_version": "landing/0.1", "result": result, "per_policy": per_policy}

    def issue_certificate(self, tenant_id: str, req: LandingIssueRequest) -> dict:
        # 1. preconditions
        if req.layer_bytes is not None and not self._layers_ok(req.layer_bytes, req.layer_hashes):
            raise LandingError("artifact layer hash mismatch")
        if not req.data_provenance.get("merkle_root"):
            raise LandingError("data provenance not sealed (no merkle_root)")

        # 2. governed gate — GovernanceGateway.evaluate_and_gate(tenant, operation, context) -> (allowed, logs)
        if self.gateway is not None:
            allowed, logs = self.gateway.evaluate_and_gate(
                tenant_id, "landing.issue",
                {"artifact_ref": req.artifact_ref, "authority": req.authority})
            if not allowed:
                if any(_result_value(l) == "escalated" for l in logs):
                    cid = self._persist_pending(tenant_id, req, "waiting_hitl")
                    raise LandingPendingHITL(cid)
                cid = self._persist_pending(tenant_id, req, "denied")
                raise LandingDenied(cid)

        # 3. build + sign  (mirrors erasure_proof.issue_certificate)
        cert = self._build(tenant_id, req)
        self._sign_inplace(cert)
        # 4. (future) anchor in a gcrumbs Merkle epoch — see epoch_anchor
        cert["anchor"] = self._anchor(tenant_id, cert) if self.epoch_anchor else None
        # 5. persist
        self._persist(tenant_id, cert, status="issued")
        return cert

    def resume(self, tenant_id: str, certificate_id: str, approved: bool, approver: str) -> dict:
        row = self.get_certificate(tenant_id, certificate_id)
        if row["status"] != "waiting_hitl":
            raise LandingError(f"not awaiting HITL (status={row['status']})")
        if not approved:
            self._set_status(tenant_id, certificate_id, "denied")
            return {"certificate_id": certificate_id, "status": "denied", "approver": approver}
        req = self._req_from_row(row)
        cert = self._build(tenant_id, req); self._sign_inplace(cert)
        cert["anchor"] = self._anchor(tenant_id, cert) if self.epoch_anchor else None
        self._persist(tenant_id, cert, status="issued")
        return cert

    def get_certificate(self, tenant_id, certificate_id):
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM landing_certificates WHERE tenant_id=%s AND certificate_id=%s",
                        (tenant_id, certificate_id))
            row = cur.fetchone()
        if not row:
            raise LandingError("certificate not found")
        return row

    def list_certificates(self, tenant_id, limit=50, offset=0):
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM landing_certificates WHERE tenant_id=%s "
                        "ORDER BY created_at DESC LIMIT %s OFFSET %s", (tenant_id, limit, offset))
            return cur.fetchall()

    def verify_certificate(self, tenant_id, certificate_id) -> dict:
        """Ed25519 signature verification (mirrors erasure_proof.verify_certificate) + data checks."""
        cert = self.get_certificate(tenant_id, certificate_id)
        checks = {"signature": self._verify_sig(cert),
                  "conformance_pass": (cert["conformance"] or {}).get("result") == "pass",
                  "authority_present": bool((cert["authority"] or {}).get("delegation_ref"))}
        a = cert["authority"] or {}
        recon = {"what": f'{cert["artifact_ref"]} on {cert["base_model_ref"]}',
                 "from_where": (cert["data_provenance"] or {}).get("corpus_hash"),
                 "under_whom": f'{a.get("human_principal")} @ {a.get("trust_tier")}',
                 "cleared_how": (cert["conformance"] or {}).get("result"),
                 "may_do": cert["permitted_actions"]}
        return {"passed": all(checks.values()), "checks": checks, "reconstruction": recon}

    def get_stats(self, tenant_id) -> dict:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, count(*) AS n FROM landing_certificates WHERE tenant_id=%s GROUP BY status", (tenant_id,))
            return {r["status"]: r["n"] for r in cur.fetchall()}

    # ---- internals ----
    def _build(self, tenant_id, req: LandingIssueRequest) -> dict:
        ts = time.time()
        cert = {"version": "lc/0.1", "tenant_id": tenant_id, "timestamp": ts,
                "artifact": {"artifact_ref": req.artifact_ref, "base_model_ref": req.base_model_ref,
                             "layer_hashes": req.layer_hashes, "kind": req.kind,
                             "manifest_digest": b2_256(canon(req.layer_hashes))},
                "data_provenance": req.data_provenance, "authority": req.authority,
                "conformance": req.conformance, "permitted_actions": req.permitted_actions}
        cert["certificate_id"] = compute_certificate_id(
            tenant_id, req.artifact_ref, req.data_provenance["merkle_root"],
            req.authority.get("delegation_ref", ""), req.conformance["result"], ts)
        return cert

    def _sign_inplace(self, cert: dict) -> None:
        if not self.signing_key:
            cert["signature"] = None; cert["signer_public_key"] = None; return
        from aml.provenance import sign_provenance           # your real signer
        signature, public_key = sign_provenance(self.signing_key, compute_certificate_digest(cert))
        cert["signature"] = signature.hex()
        cert["signer_public_key"] = public_key.hex()

    def _verify_sig(self, cert) -> bool:
        if not cert.get("signature") or not cert.get("signer_public_key"):
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(cert["signer_public_key"]))
            pub.verify(bytes.fromhex(cert["signature"]), compute_certificate_digest(cert))
            return True
        except Exception:
            return False

    def _anchor(self, tenant_id, cert) -> Optional[dict]:
        # FUTURE (epoch_anchor=True): emit a gcrumbs breadcrumb for cert['certificate_id'],
        # roll_epoch(agent_id=tenant), get_proof((epoch_id, leaf_index)) -> inclusion proof.
        return None

    def _layers_ok(self, layer_bytes, layer_hashes) -> bool:
        return all(b2_256(b) == h for b, h in zip(layer_bytes, layer_hashes))

    def _persist(self, tenant_id, cert, status):
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO landing_certificates
                (certificate_id, tenant_id, artifact_ref, base_model_ref, layer_hashes,
                 data_provenance, authority, conformance, permitted_actions, anchor, status,
                 signature, signer_public_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (certificate_id) DO UPDATE SET status=EXCLUDED.status,
                  anchor=EXCLUDED.anchor, signature=EXCLUDED.signature,
                  signer_public_key=EXCLUDED.signer_public_key""",
                (cert["certificate_id"], tenant_id, cert["artifact"]["artifact_ref"],
                 cert["artifact"]["base_model_ref"], json.dumps(cert["artifact"]["layer_hashes"]),
                 json.dumps(cert["data_provenance"]), json.dumps(cert["authority"]),
                 json.dumps(cert["conformance"]), json.dumps(cert["permitted_actions"]),
                 json.dumps(cert.get("anchor")), status, cert.get("signature"), cert.get("signer_public_key")))

    def _persist_pending(self, tenant_id, req: LandingIssueRequest, status) -> str:
        cid = compute_certificate_id(tenant_id, req.artifact_ref,
                                     req.data_provenance.get("merkle_root", ""),
                                     req.authority.get("delegation_ref", ""), status, time.time())
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO landing_certificates
                (certificate_id, tenant_id, artifact_ref, base_model_ref, layer_hashes,
                 data_provenance, authority, conformance, permitted_actions, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (certificate_id) DO NOTHING""",
                (cid, tenant_id, req.artifact_ref, req.base_model_ref, json.dumps(req.layer_hashes),
                 json.dumps(req.data_provenance), json.dumps(req.authority), json.dumps(req.conformance),
                 json.dumps(req.permitted_actions), status))
        return cid

    def _set_status(self, tenant_id, certificate_id, status):
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE landing_certificates SET status=%s WHERE tenant_id=%s AND certificate_id=%s",
                        (status, tenant_id, certificate_id))

    def _req_from_row(self, row) -> LandingIssueRequest:
        return LandingIssueRequest(
            artifact_ref=row["artifact_ref"], base_model_ref=row["base_model_ref"],
            layer_hashes=row["layer_hashes"], data_provenance=row["data_provenance"],
            authority=row["authority"], conformance=row["conformance"],
            permitted_actions=row["permitted_actions"])
