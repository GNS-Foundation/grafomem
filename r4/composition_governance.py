"""
src/aml/cloud/composition_governance.py   (R4 — Composition Governance)

Governs how certified artifacts may COMBINE. A composition proposes a set of members — each a
reference to a certified artifact (a Landing certificate / registered artifact) with its license
— plus a composition kind and a target ref. The service enforces composition policy:

  1. every member must be CERTIFIED (an uncertified member is refused),
  2. member licenses must be mutually COMPATIBLE (no no-derivatives; no non-commercial mixed
     with commercial),
  3. the composer's trust tier must meet the composition's requirement,

then runs the GovernanceGateway and, on approval, issues a signed composition receipt attesting
that these certified artifacts may compose under this policy. The target ref can be fed back into
R1 to register the composed artifact — closing the cycle:

    R2 corpus -> R1 artifact -> R3 certificate -> R4 composition -> (R1 register the composed result)

Mirrors the other services: db_url + signing_key, _get_conn, ensure_schema, sign via
aml.provenance, document-column verify, string timestamp, Jsonb binding, deny/HITL gateway.
"""
from __future__ import annotations
import hashlib, time
from dataclasses import dataclass, field
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

US = b"\x1f"

TIER_RANK = {"untrusted": 0, "basic": 1, "verified": 2, "trusted": 3, "release": 4, "root": 5}
def _rank(tier: str) -> int:
    return TIER_RANK.get(tier or "untrusted", 0)

def canon(obj) -> bytes:
    import json
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()

def b2_256(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()

def compute_composition_id(tenant_id: str, kind: str, member_ids: list[str]) -> str:
    """BLAKE2b-128 — content-addressed over (tenant, kind, member ids); order-independent (sorts internally)."""
    h = hashlib.blake2b(digest_size=16)
    h.update(US.join([tenant_id.encode(), kind.encode(), canon(sorted(member_ids))]))
    return h.hexdigest()

def licenses_compatible(licenses: list[str]) -> tuple[bool, str]:
    """Lightweight composition-license policy (pluggable). Empty = compatible."""
    lowers = [(l or "").lower() for l in licenses]
    nd = [l for l in lowers if "nd" in l.split("-") or "no-deriv" in l or "noderiv" in l or "noderivatives" in l]
    if nd:
        return False, f"no-derivatives license cannot be composed: {nd}"
    has_nc = any("nc" in l.split("-") or "non-commercial" in l or "noncommercial" in l for l in lowers)
    has_comm = any(("commercial" in l and "non" not in l) for l in lowers)
    if has_nc and has_comm:
        return False, "non-commercial license mixed with commercial license"
    return True, "compatible"

_SIGNED = ["schema_version", "tenant_id", "timestamp", "composition_kind", "member_ids",
           "members_digest", "license_verdict", "target_ref", "authority", "composition_id"]

def _receipt_digest(doc: dict) -> bytes:
    return hashlib.blake2b(canon({k: doc[k] for k in _SIGNED}), digest_size=32).digest()

def _result_value(log) -> str:
    r = getattr(log, "result", log)
    return getattr(r, "value", r)


@dataclass
class ComposeRequest:
    composition_kind: str                       # e.g. lora-stack | ensemble | adapter+base
    members: list                               # [{ref_id, license, certified: bool}]
    target_ref: str                             # composed artifact ref, e.g. oci://.../composed:1
    authority: dict = field(default_factory=dict)
    required_trust_tier: str = "verified"


class CompositionError(Exception): ...
class ComposeRejected(CompositionError):
    def __init__(self, reasons): self.reasons = reasons; super().__init__("; ".join(reasons))
class ComposePendingHITL(CompositionError):
    def __init__(self, composition_id): self.composition_id = composition_id; super().__init__(composition_id)


class CompositionGovernanceService:
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
            CREATE TABLE IF NOT EXISTS compositions (
              composition_id    TEXT PRIMARY KEY,
              tenant_id         TEXT NOT NULL,
              composition_kind  TEXT NOT NULL,
              target_ref        TEXT NOT NULL,
              member_count      INT  NOT NULL,
              document          JSONB,
              status            TEXT NOT NULL DEFAULT 'governed',
              signature         TEXT,
              signer_public_key TEXT,
              created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            ALTER TABLE compositions ADD COLUMN IF NOT EXISTS document JSONB;
            CREATE INDEX IF NOT EXISTS ix_compositions_tenant ON compositions(tenant_id);
            """)

    # ---- surface ----
    def compose(self, tenant_id: str, req: ComposeRequest) -> dict:
        if not req.members:
            raise CompositionError("composition has no members")

        # 1. every member must be certified
        uncertified = [m.get("ref_id") for m in req.members if not m.get("certified")]
        if uncertified:
            raise ComposeRejected([f"member not certified: {r}" for r in uncertified])

        # 2. licenses must be mutually compatible
        ok, detail = licenses_compatible([m.get("license", "") for m in req.members])
        if not ok:
            raise ComposeRejected([detail])

        # 3. composer authority must meet the requirement
        caller = (req.authority or {}).get("trust_tier", "untrusted")
        if _rank(caller) < _rank(req.required_trust_tier):
            raise ComposeRejected([f"insufficient trust tier: '{caller}' < required '{req.required_trust_tier}'"])

        member_ids = sorted(m["ref_id"] for m in req.members)
        composition_id = compute_composition_id(tenant_id, req.composition_kind, member_ids)

        # 4. governance gate
        if self.gateway is not None:
            allowed, logs = self.gateway.evaluate_and_gate(
                tenant_id, "composition.govern",
                {"kind": req.composition_kind, "members": member_ids, "target": req.target_ref})
            if not allowed:
                if any(_result_value(l) == "escalated" for l in logs):
                    self._persist_pending(tenant_id, req, composition_id, "waiting_hitl")
                    raise ComposePendingHITL(composition_id)
                self._persist_pending(tenant_id, req, composition_id, "denied")
                raise ComposeRejected(["denied by governance"])

        return self._emit(tenant_id, req, member_ids, composition_id, {"compatible": ok, "detail": detail})

    def resume(self, tenant_id, composition_id, approved: bool, approver: str) -> dict:
        row = self.get(tenant_id, composition_id)
        if row["status"] != "waiting_hitl":
            raise CompositionError(f"not awaiting HITL (status={row['status']})")
        if not approved:
            self._set_status(tenant_id, composition_id, "denied")
            return {"composition_id": composition_id, "status": "denied", "approver": approver}
        d = row["document"] or {}
        req = ComposeRequest(composition_kind=row["composition_kind"], members=d.get("members", []),
                             target_ref=row["target_ref"], authority=d.get("authority", {}))
        member_ids = sorted(m["ref_id"] for m in req.members)
        ok, detail = licenses_compatible([m.get("license", "") for m in req.members])
        return self._emit(tenant_id, req, member_ids, composition_id, {"compatible": ok, "detail": detail})

    def get(self, tenant_id, composition_id) -> dict:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM compositions WHERE tenant_id=%s AND composition_id=%s", (tenant_id, composition_id))
            row = cur.fetchone()
        if not row:
            raise CompositionError("composition not found")
        return row

    def list_compositions(self, tenant_id, limit=50, offset=0) -> list:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM compositions WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                        (tenant_id, limit, offset))
            return cur.fetchall()

    def verify(self, tenant_id, composition_id) -> dict:
        row = self.get(tenant_id, composition_id)
        doc = row.get("document")
        if not doc:
            return {"passed": False, "checks": {"signature": False, "members_consistent": False}, "status": row.get("status")}
        recomputed = b2_256(canon(doc.get("members", [])))
        checks = {"signature": self._verify_sig(doc),
                  "members_consistent": recomputed == doc.get("members_digest"),
                  "licenses_compatible": (doc.get("license_verdict") or {}).get("compatible") is True}
        a = doc.get("authority") or {}
        recon = {"kind": doc.get("composition_kind"), "members": doc.get("member_ids"),
                 "target": doc.get("target_ref"), "by": f'{a.get("human_principal")} @ {a.get("trust_tier")}',
                 "licenses": (doc.get("license_verdict") or {}).get("detail")}
        return {"passed": all(checks.values()), "checks": checks, "attestation": recon, "status": row.get("status")}

    def composed_artifact(self, tenant_id, composition_id) -> dict:
        """The descriptor to register the composed result back in R1 (closes the cycle)."""
        doc = self.get(tenant_id, composition_id).get("document") or {}
        return {"artifact_ref": doc.get("target_ref"), "composition_id": composition_id,
                "members": doc.get("member_ids"), "kind": doc.get("composition_kind")}

    def get_stats(self, tenant_id) -> dict:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, count(*) AS n FROM compositions WHERE tenant_id=%s GROUP BY status", (tenant_id,))
            return {r["status"]: r["n"] for r in cur.fetchall()}

    # ---- internals ----
    def _emit(self, tenant_id, req: ComposeRequest, member_ids, composition_id, verdict) -> dict:
        doc = {"schema_version": "comp/0.1", "tenant_id": tenant_id, "timestamp": f"{time.time():.6f}",
               "composition_kind": req.composition_kind, "members": req.members, "member_ids": member_ids,
               "members_digest": b2_256(canon(req.members)), "license_verdict": verdict,
               "target_ref": req.target_ref, "authority": req.authority or {}, "composition_id": composition_id}
        self._sign_inplace(doc)
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO compositions
                (composition_id, tenant_id, composition_kind, target_ref, member_count, document, status,
                 signature, signer_public_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (composition_id) DO UPDATE SET document=EXCLUDED.document, status=EXCLUDED.status,
                  signature=EXCLUDED.signature, signer_public_key=EXCLUDED.signer_public_key""",
                (composition_id, tenant_id, req.composition_kind, req.target_ref, len(req.members),
                 Jsonb(doc), "governed", doc.get("signature"), doc.get("signer_public_key")))
        return self.get(tenant_id, composition_id)

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

    def _persist_pending(self, tenant_id, req, composition_id, status):
        doc = {"members": req.members, "authority": req.authority or {}}
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO compositions
                (composition_id, tenant_id, composition_kind, target_ref, member_count, document, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (composition_id) DO NOTHING""",
                (composition_id, tenant_id, req.composition_kind, req.target_ref, len(req.members),
                 Jsonb(doc), status))

    def _set_status(self, tenant_id, composition_id, status):
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE compositions SET status=%s WHERE tenant_id=%s AND composition_id=%s",
                        (status, tenant_id, composition_id))
