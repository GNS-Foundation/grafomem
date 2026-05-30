"""
src/aml/cloud/provenance_customs.py   (R2 — Data-Provenance Customs / EU AI Act Article 10)

The upstream customs checkpoint for training data. Register a corpus (its sources, licensing /
lawful basis, processing, and Article-10 attestations), run the customs inspection (refuse to
seal data lacking a lawful basis or a bias examination), seal it into a Merkle root, and issue a
signed customs receipt. The sealed `merkle_root` + `corpus_hash` are exactly what R3's landing
`data_provenance` block references — so this service is the source end of the pipeline:

    R2 seal corpus -> R1 register artifact -> R3 issue certificate -> R5 govern world-model

Distinctive piece: a real Merkle tree over the corpus sources with O(log n) inclusion proofs
(the proofs landing's epoch_anchor deferred). Mirrors the other services otherwise: db_url +
signing_key, _get_conn, ensure_schema, sign via aml.provenance, document-column verify, Jsonb.
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

# ---- Merkle tree (BLAKE2b-256, domain-separated leaf/node prefixes) ----
def _leaf(source: dict) -> bytes:
    return hashlib.blake2b(b"\x00" + canon(source), digest_size=32).digest()

def _node(a: bytes, b: bytes) -> bytes:
    return hashlib.blake2b(b"\x01" + a + b, digest_size=32).digest()

def merkle_root(leaves: list[bytes]) -> bytes:
    if not leaves:
        return hashlib.blake2b(b"\x02empty", digest_size=32).digest()
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])                       # duplicate last if odd
        level = [_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]

def merkle_proof(leaves: list[bytes], index: int) -> list[tuple[str, str]]:
    """Returns [(sibling_hex, 'L'|'R')] from leaf up to root."""
    proof, level, idx = [], list(leaves), index
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sib = idx ^ 1
        proof.append((level[sib].hex(), "L" if sib < idx else "R"))
        level = [_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        idx //= 2
    return proof

def verify_merkle_proof(leaf: bytes, proof: list[tuple[str, str]], root: bytes) -> bool:
    h = leaf
    for sib_hex, side in proof:
        sib = bytes.fromhex(sib_hex)
        h = _node(sib, h) if side == "L" else _node(h, sib)
    return h == root

# ---- ids ----
def compute_corpus_id(tenant_id: str, name: str, merkle_root_hex: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(US.join(s.encode() for s in (tenant_id, name, merkle_root_hex)))
    return h.hexdigest()

_SIGNED = ["schema_version", "tenant_id", "timestamp", "name", "sources_digest",
           "merkle_root", "corpus_hash", "attestations", "clearance", "corpus_id"]

def _receipt_digest(doc: dict) -> bytes:
    return hashlib.blake2b(canon({k: doc[k] for k in _SIGNED}), digest_size=32).digest()

def _result_value(log) -> str:
    r = getattr(log, "result", log)
    return getattr(r, "value", r)

def article10_reasons(sources: list, attestations: dict) -> list[str]:
    """Customs inspection — empty list means cleared."""
    reasons = []
    for s in sources:
        if not (s.get("license") or s.get("lawful_basis")):
            reasons.append(f"source '{s.get('id')}' has no license or lawful_basis")
    if not (attestations or {}).get("representativeness"):
        reasons.append("missing representativeness attestation (Art.10 §3)")
    if not (attestations or {}).get("bias_examination"):
        reasons.append("missing bias_examination attestation (Art.10 §2(f))")
    return reasons


@dataclass
class CorpusRegisterRequest:
    name: str
    sources: list                              # [{id, license|lawful_basis, content_hash, record_count, ...}]
    attestations: dict = field(default_factory=dict)   # {representativeness, bias_examination, data_gaps}
    processing: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class CustomsError(Exception): ...
class CustomsRejected(CustomsError):
    def __init__(self, reasons): self.reasons = reasons; super().__init__("; ".join(reasons))


class ProvenanceCustomsService:
    def __init__(self, db_url: str, *, signing_key: Optional[bytes] = None,
                 gateway=None, decision_trail=None):
        self.db_url = db_url
        self.signing_key = signing_key
        self.gateway = gateway
        self.decision_trail = decision_trail

    # ---- db ----
    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.db_url, row_factory=dict_row, autocommit=True)

    def ensure_schema(self) -> None:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS provenance_corpora (
              corpus_id      TEXT PRIMARY KEY,
              tenant_id      TEXT NOT NULL,
              name           TEXT NOT NULL,
              merkle_root    TEXT NOT NULL,
              corpus_hash    TEXT NOT NULL,
              source_count   INT  NOT NULL,
              clearance      TEXT NOT NULL,
              document       JSONB,
              status         TEXT NOT NULL DEFAULT 'sealed',
              signature      TEXT,
              signer_public_key TEXT,
              created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
              UNIQUE (tenant_id, name, merkle_root)
            );
            ALTER TABLE provenance_corpora ADD COLUMN IF NOT EXISTS document JSONB;
            CREATE INDEX IF NOT EXISTS ix_corpora_tenant ON provenance_corpora(tenant_id);
            """)

    # ---- customs surface ----
    def register_corpus(self, tenant_id: str, req: CorpusRegisterRequest) -> dict:
        if not req.sources:
            raise CustomsError("corpus has no sources")

        # 1. Article-10 inspection — refuse to seal uncleared data
        reasons = article10_reasons(req.sources, req.attestations)
        if reasons:
            raise CustomsRejected(reasons)

        # 2. governance gate
        if self.gateway is not None:
            allowed, logs = self.gateway.evaluate_and_gate(
                tenant_id, "provenance.seal", {"name": req.name, "sources": len(req.sources)})
            if not allowed:
                raise CustomsRejected(["sealing denied by governance"])

        # 3. seal into a Merkle root
        leaves = [_leaf(s) for s in req.sources]
        root_hex = merkle_root(leaves).hex()
        sources_digest = b2_256(canon(req.sources))
        corpus_id = compute_corpus_id(tenant_id, req.name, root_hex)
        doc = {"schema_version": "customs/0.1", "tenant_id": tenant_id, "timestamp": f"{time.time():.6f}",
               "name": req.name, "sources": req.sources, "sources_digest": sources_digest,
               "processing": req.processing, "attestations": req.attestations,
               "merkle_root": root_hex, "clearance": "cleared", "corpus_id": corpus_id,
               "metadata": req.metadata or {}}
        doc["corpus_hash"] = b2_256(canon({k: doc[k] for k in
                              ("name", "sources", "processing", "attestations", "merkle_root")}))
        self._sign_inplace(doc)
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO provenance_corpora
                (corpus_id, tenant_id, name, merkle_root, corpus_hash, source_count, clearance,
                 document, status, signature, signer_public_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (corpus_id) DO UPDATE SET document=EXCLUDED.document,
                  signature=EXCLUDED.signature, signer_public_key=EXCLUDED.signer_public_key""",
                (corpus_id, tenant_id, req.name, root_hex, doc["corpus_hash"], len(req.sources),
                 "cleared", Jsonb(doc), "sealed", doc.get("signature"), doc.get("signer_public_key")))
        return self.get_corpus(tenant_id, corpus_id)

    def get_corpus(self, tenant_id, corpus_id) -> dict:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM provenance_corpora WHERE tenant_id=%s AND corpus_id=%s", (tenant_id, corpus_id))
            row = cur.fetchone()
        if not row:
            raise CustomsError("corpus not found")
        return row

    def list_corpora(self, tenant_id, limit=50, offset=0) -> list:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM provenance_corpora WHERE tenant_id=%s "
                        "ORDER BY created_at DESC LIMIT %s OFFSET %s", (tenant_id, limit, offset))
            return cur.fetchall()

    def verify_corpus(self, tenant_id, corpus_id) -> dict:
        """Signature + Merkle-root recomputation consistency."""
        row = self.get_corpus(tenant_id, corpus_id)
        doc = row.get("document")
        if not doc:
            return {"passed": False, "checks": {"signature": False, "merkle_consistent": False}}
        recomputed = merkle_root([_leaf(s) for s in doc["sources"]]).hex()
        checks = {"signature": self._verify_sig(doc),
                  "merkle_consistent": recomputed == doc["merkle_root"],
                  "cleared": doc.get("clearance") == "cleared"}
        return {"passed": all(checks.values()), "checks": checks,
                "merkle_root": doc["merkle_root"], "corpus_hash": doc["corpus_hash"]}

    def inclusion_proof(self, tenant_id, corpus_id, source_id: str) -> dict:
        """Prove a source is part of the sealed corpus (Merkle inclusion proof)."""
        doc = self.get_corpus(tenant_id, corpus_id).get("document") or {}
        sources = doc.get("sources", [])
        idx = next((i for i, s in enumerate(sources) if s.get("id") == source_id), None)
        if idx is None:
            return {"included": False, "reason": "source not in corpus"}
        leaves = [_leaf(s) for s in sources]
        proof = merkle_proof(leaves, idx)
        root = bytes.fromhex(doc["merkle_root"])
        return {"included": verify_merkle_proof(leaves[idx], proof, root),
                "leaf": leaves[idx].hex(), "proof": proof, "merkle_root": doc["merkle_root"]}

    def provenance_block(self, tenant_id, corpus_id) -> dict:
        """The data_provenance dict R3's landing.issue expects (so R2 feeds R3)."""
        doc = self.get_corpus(tenant_id, corpus_id).get("document") or {}
        return {"merkle_root": doc.get("merkle_root"), "corpus_hash": doc.get("corpus_hash"),
                "sources": [s.get("id") for s in doc.get("sources", [])], "corpus_id": corpus_id}

    def get_stats(self, tenant_id) -> dict:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT clearance, count(*) AS n FROM provenance_corpora WHERE tenant_id=%s GROUP BY clearance", (tenant_id,))
            return {r["clearance"]: r["n"] for r in cur.fetchall()}

    # ---- internals ----
    def _sign_inplace(self, doc: dict) -> None:
        if not self.signing_key:
            doc["signature"] = None; doc["signer_public_key"] = None; return
        from aml.provenance import sign_provenance
        signature, public_key = sign_provenance(self.signing_key, _receipt_digest(doc))
        doc["signature"] = signature.hex()
        doc["signer_public_key"] = public_key.hex()

    def _verify_sig(self, doc) -> bool:
        if not doc.get("signature") or not doc.get("signer_public_key"):
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(doc["signer_public_key"]))
            pub.verify(bytes.fromhex(doc["signature"]), _receipt_digest(doc))
            return True
        except Exception:
            return False
