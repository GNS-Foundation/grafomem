"""
src/aml/cloud/artifact_registry.py   (R1 — Adaptation Artifact Registry)

The intake side of the Governance Airport. Registers an adaptation artifact
(OCI + ModelPack manifest: base model + layers) under a content-addressed,
deterministic BLAKE2b-128 id, issues an Ed25519-signed registration receipt
(stored as the exact JSONB document, verified against itself), gates registration
through the GovernanceGateway, verifies layer integrity against supplied bytes,
and links forward to a Landing Certificate (R3) once one is issued.

Mirrors cloud/landing_service.py / erasure_proof.py: db_url + signing_key, _get_conn,
ensure_schema, sign via aml.provenance.sign_provenance, Jsonb binding, string timestamp.
"""
from __future__ import annotations
import hashlib, time
from dataclasses import dataclass, field
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

US = b"\x1f"

def canon(obj) -> bytes:
    import json
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()

def b2_256(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()

def compute_manifest_digest(base_model_ref: str, kind: str, layer_hashes: list[str]) -> str:
    """BLAKE2b-256 over the canonical (base model, kind, ordered layer hashes)."""
    return b2_256(canon({"base_model_ref": base_model_ref, "kind": kind, "layer_hashes": layer_hashes}))

def compute_artifact_id(tenant_id: str, artifact_ref: str, base_model_ref: str, manifest_digest: str) -> str:
    """BLAKE2b-128 — content-addressed (no timestamp), so re-registering identical content is idempotent."""
    h = hashlib.blake2b(digest_size=16)
    h.update(US.join(s.encode() for s in (tenant_id, artifact_ref, base_model_ref, manifest_digest)))
    return h.hexdigest()

# fields covered by the registration-receipt signature
_SIGNED = ["schema_version", "tenant_id", "timestamp", "artifact_ref", "base_model_ref",
           "kind", "layers", "layer_hashes", "manifest_digest", "metadata", "artifact_id"]

def compute_receipt_digest(doc: dict) -> bytes:
    return hashlib.blake2b(canon({k: doc[k] for k in _SIGNED}), digest_size=32).digest()


def _result_value(log) -> str:
    r = getattr(log, "result", log)
    return getattr(r, "value", r)


@dataclass
class ArtifactRegisterRequest:
    artifact_ref: str                       # e.g. oci://registry/org/name:tag
    base_model_ref: str                     # e.g. llama-3.1-8b@sha256:...
    layers: list                            # [{media_type, digest, size}] — digest is the b2_256 content hash
    kind: str = "lora+rag"
    metadata: dict = field(default_factory=dict)
    layer_bytes: Optional[list] = field(default=None, repr=False)   # optional, verified at registration


class RegistryError(Exception): ...
class RegistryDenied(RegistryError): ...
class RegistryPendingHITL(RegistryError):
    def __init__(self, artifact_id): self.artifact_id = artifact_id; super().__init__(artifact_id)


class ArtifactRegistryService:
    """Mirrors LandingService(db_url, ..., signing_key=...)."""

    def __init__(self, db_url: str, *, signing_identity=None,
                 gateway=None, decision_trail=None, pool=None):
        self.db_url = db_url
        self.signing_identity = signing_identity
        self.gateway = gateway
        self.decision_trail = decision_trail
        self._pool = pool

    # ---- db ----
    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._pool is not None:
            return self._pool.getconn()
        return psycopg.connect(self.db_url, row_factory=dict_row, autocommit=True)

    def _put_conn(self, conn):
        if self._pool is not None:
            self._pool.putconn(conn)

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS artifact_registry (
                  artifact_id       TEXT PRIMARY KEY,
                  tenant_id         TEXT NOT NULL,
                  artifact_ref      TEXT NOT NULL,
                  base_model_ref    TEXT NOT NULL,
                  kind              TEXT NOT NULL,
                  manifest_digest   TEXT NOT NULL,
                  layer_hashes      JSONB NOT NULL,
                  metadata          JSONB,
                  document          JSONB,          -- the exact signed registration receipt
                  status            TEXT NOT NULL DEFAULT 'registered',
                  certificate_id    TEXT,           -- set when R3 certifies this artifact
                  signature         TEXT,
                  signer_public_key TEXT,
                  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                ALTER TABLE artifact_registry ADD COLUMN IF NOT EXISTS document JSONB;
                CREATE INDEX IF NOT EXISTS ix_artifacts_tenant ON artifact_registry(tenant_id);
                CREATE INDEX IF NOT EXISTS ix_artifacts_ref    ON artifact_registry(tenant_id, artifact_ref);
                """)
        finally:
            self._put_conn(conn)

    # ---- R1 surface ----
    def register(self, tenant_id: str, req: ArtifactRegisterRequest) -> dict:
        layer_hashes = [l["digest"] for l in req.layers]

        # 1. precondition — if bytes supplied, they must match the declared digests
        if req.layer_bytes is not None and not self._layers_ok(req.layer_bytes, layer_hashes):
            raise RegistryError("artifact layer hash mismatch")

        manifest_digest = compute_manifest_digest(req.base_model_ref, req.kind, layer_hashes)
        artifact_id = compute_artifact_id(tenant_id, req.artifact_ref, req.base_model_ref, manifest_digest)

        # 2. idempotent — identical content returns the existing receipt
        existing = self._get(tenant_id, artifact_id)
        if existing:
            return existing

        # 3. governed gate
        if self.gateway is not None:
            allowed, logs = self.gateway.evaluate_and_gate(
                tenant_id, "artifact.register",
                {"artifact_ref": req.artifact_ref, "base_model_ref": req.base_model_ref})
            if not allowed:
                if any(_result_value(l) == "escalated" for l in logs):
                    self._persist_pending(tenant_id, req, layer_hashes, manifest_digest, artifact_id, "waiting_hitl")
                    raise RegistryPendingHITL(artifact_id)
                self._persist_pending(tenant_id, req, layer_hashes, manifest_digest, artifact_id, "denied")
                raise RegistryDenied(artifact_id)

        # 4. build receipt + sign + persist
        doc = self._build(tenant_id, req, layer_hashes, manifest_digest, artifact_id)
        self._sign_inplace(doc)
        self._persist(tenant_id, doc, status="registered")
        return self.get(tenant_id, artifact_id)   # consistent row shape (has status, document, etc.)

    def resume(self, tenant_id: str, artifact_id: str, approved: bool, approver: str) -> dict:
        row = self.get(tenant_id, artifact_id)
        if row["status"] != "waiting_hitl":
            raise RegistryError(f"not awaiting HITL (status={row['status']})")
        if not approved:
            self._set_status(tenant_id, artifact_id, "denied")
            return {"artifact_id": artifact_id, "status": "denied", "approver": approver}
        req = self._req_from_row(row)
        layer_hashes = row["layer_hashes"]
        doc = self._build(tenant_id, req, layer_hashes, row["manifest_digest"], artifact_id)
        self._sign_inplace(doc)
        self._persist(tenant_id, doc, status="registered")
        return self.get(tenant_id, artifact_id)

    def get(self, tenant_id, artifact_id) -> dict:
        row = self._get(tenant_id, artifact_id)
        if not row:
            raise RegistryError("artifact not found")
        return row

    def get_by_ref(self, tenant_id, artifact_ref) -> list:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM artifact_registry WHERE tenant_id=%s AND artifact_ref=%s "
                            "ORDER BY created_at DESC", (tenant_id, artifact_ref))
                return cur.fetchall()
        finally:
            self._put_conn(conn)

    def list_artifacts(self, tenant_id, limit=50, offset=0) -> list:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM artifact_registry WHERE tenant_id=%s "
                            "ORDER BY created_at DESC LIMIT %s OFFSET %s", (tenant_id, limit, offset))
                return cur.fetchall()
        finally:
            self._put_conn(conn)

    def verify(self, tenant_id, artifact_id) -> dict:
        """Verify the registration receipt: Ed25519 signature + manifest digest self-consistency."""
        row = self.get(tenant_id, artifact_id)
        doc = row.get("document")
        if not doc:
            return {"passed": False, "checks": {"signature": False, "manifest_consistent": False}, "status": row.get("status")}
        recomputed = compute_manifest_digest(doc["base_model_ref"], doc["kind"], doc["layer_hashes"])
        checks = {"signature": self._verify_sig(doc),
                  "manifest_consistent": recomputed == doc["manifest_digest"]}
        return {"passed": all(checks.values()), "checks": checks,
                "manifest_digest": doc["manifest_digest"], "status": row.get("status")}

    def check_integrity(self, tenant_id, artifact_id, layer_hashes: list[str]) -> dict:
        """Compare supplied layer hashes (e.g. recomputed from the actual bytes) to the registered manifest."""
        doc = self.get(tenant_id, artifact_id).get("document") or {}
        registered = doc.get("layer_hashes", [])
        match = list(layer_hashes) == list(registered)
        return {"match": match, "registered": registered, "supplied": list(layer_hashes)}

    def certify(self, tenant_id, artifact_id, certificate_id: str) -> dict:
        """Link a Landing Certificate (R3) to this artifact and flip status -> certified."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE artifact_registry SET status='certified', certificate_id=%s "
                            "WHERE tenant_id=%s AND artifact_id=%s",
                            (certificate_id, tenant_id, artifact_id))
        finally:
            self._put_conn(conn)
        return self.get(tenant_id, artifact_id)

    def get_stats(self, tenant_id) -> dict:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT status, count(*) AS n FROM artifact_registry WHERE tenant_id=%s GROUP BY status", (tenant_id,))
                return {r["status"]: r["n"] for r in cur.fetchall()}
        finally:
            self._put_conn(conn)

    # ---- internals ----
    def _build(self, tenant_id, req: ArtifactRegisterRequest, layer_hashes, manifest_digest, artifact_id) -> dict:
        return {
            "schema_version": "modelpack/0.1", "tenant_id": tenant_id, "timestamp": f"{time.time():.6f}",
            "artifact_ref": req.artifact_ref, "base_model_ref": req.base_model_ref, "kind": req.kind,
            "layers": req.layers, "layer_hashes": layer_hashes, "manifest_digest": manifest_digest,
            "metadata": req.metadata or {}, "artifact_id": artifact_id,
        }

    def _sign_inplace(self, doc: dict) -> None:
        if not self.signing_identity:
            doc["signature"] = None; doc["signer_public_key"] = None; return
        from aml.provenance import sign_provenance
        signature, public_key = sign_provenance(self.signing_identity, compute_receipt_digest(doc))
        doc["signature"] = signature.hex()
        doc["signer_public_key"] = public_key.hex()

    def _verify_sig(self, doc) -> bool:
        if not doc.get("signature") or not doc.get("signer_public_key"):
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(doc["signer_public_key"]))
            pub.verify(bytes.fromhex(doc["signature"]), compute_receipt_digest(doc))
            return True
        except Exception:
            return False

    def _layers_ok(self, layer_bytes, layer_hashes) -> bool:
        return all(b2_256(b) == h for b, h in zip(layer_bytes, layer_hashes))

    def _get(self, tenant_id, artifact_id) -> Optional[dict]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM artifact_registry WHERE tenant_id=%s AND artifact_id=%s",
                            (tenant_id, artifact_id))
                return cur.fetchone()
        finally:
            self._put_conn(conn)

    def _persist(self, tenant_id, doc, status):
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO artifact_registry
                    (artifact_id, tenant_id, artifact_ref, base_model_ref, kind, manifest_digest,
                     layer_hashes, metadata, document, status, signature, signer_public_key)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (artifact_id) DO UPDATE SET status=EXCLUDED.status,
                      document=EXCLUDED.document, signature=EXCLUDED.signature,
                      signer_public_key=EXCLUDED.signer_public_key""",
                    (doc["artifact_id"], tenant_id, doc["artifact_ref"], doc["base_model_ref"], doc["kind"],
                     doc["manifest_digest"], Jsonb(doc["layer_hashes"]), Jsonb(doc["metadata"]),
                     Jsonb(doc), status, doc.get("signature"), doc.get("signer_public_key")))
        finally:
            self._put_conn(conn)

    def _persist_pending(self, tenant_id, req, layer_hashes, manifest_digest, artifact_id, status):
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO artifact_registry
                    (artifact_id, tenant_id, artifact_ref, base_model_ref, kind, manifest_digest,
                     layer_hashes, metadata, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (artifact_id) DO NOTHING""",
                    (artifact_id, tenant_id, req.artifact_ref, req.base_model_ref, req.kind,
                     manifest_digest, Jsonb(layer_hashes), Jsonb(req.metadata or {}), status))
        finally:
            self._put_conn(conn)

    def _set_status(self, tenant_id, artifact_id, status):
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE artifact_registry SET status=%s WHERE tenant_id=%s AND artifact_id=%s",
                            (status, tenant_id, artifact_id))
        finally:
            self._put_conn(conn)

    def _req_from_row(self, row) -> ArtifactRegisterRequest:
        doc = row.get("document") or {}
        layers = doc.get("layers") or [{"media_type": "application/octet-stream", "digest": h, "size": 0}
                                       for h in row["layer_hashes"]]
        return ArtifactRegisterRequest(
            artifact_ref=row["artifact_ref"], base_model_ref=row["base_model_ref"],
            layers=layers, kind=row["kind"], metadata=row.get("metadata") or {})
