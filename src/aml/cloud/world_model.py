"""
src/aml/cloud/world_model.py   (R5 — Governed World-Model)

A typed graph that is governed, not merely stored. Three things:

  1. A signed TYPE REGISTRY — Object / Link / Action type definitions, each a tamper-evident
     signed schema receipt with a deterministic id.
  2. VALIDATION — object instances against their Object type; links against their from/to types.
  3. GOVERNED ACTION INVOCATION — invoking an Action type runs the caller's authority/trust-tier
     against the action's requirement, runs the GovernanceGateway, and on success emits a signed,
     attributable invocation receipt (who did what action to which objects, under which decision).
     This is the crypto-governed Action Type — the differentiator vs an ungoverned typed ontology.

Mirrors landing/registry: db_url + signing_key, _get_conn, ensure_schema, sign via
aml.provenance.sign_provenance, document-column verify, string timestamp, Jsonb binding.

gcrumbs note: unlike the certificates, an action invocation is step-shaped, so this is the
natural place to later chain via execution_receipts.issue_receipt(...) — left as a hook (sign-only v1).
"""
from __future__ import annotations
import hashlib, json, time
from dataclasses import dataclass, field
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

US = b"\x1f"

# GNS / TierGate-style trust ordering; unknown -> untrusted (0)
TIER_RANK = {"untrusted": 0, "basic": 1, "verified": 2, "trusted": 3, "release": 4, "root": 5}
def _rank(tier: str) -> int:
    return TIER_RANK.get(tier or "untrusted", 0)

def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()

def b2_256(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()

def compute_type_id(tenant_id: str, kind: str, name: str) -> str:
    """BLAKE2b-128 — content-addressed by (tenant, kind, name); stable across schema revisions."""
    h = hashlib.blake2b(digest_size=16)
    h.update(US.join(s.encode() for s in (tenant_id, kind, name)))
    return h.hexdigest()

def compute_schema_digest(spec: dict) -> str:
    return b2_256(canon(spec))

def compute_action_id(tenant_id: str, action_name: str, subject_refs: list, ts: str) -> str:
    """BLAKE2b-128 — unique per invocation (ts included): each action event is distinct."""
    h = hashlib.blake2b(digest_size=16)
    h.update(US.join([tenant_id.encode(), action_name.encode(), canon(subject_refs), ts.encode()]))
    return h.hexdigest()

_SIGNED_TYPE = ["schema_version", "tenant_id", "timestamp", "kind", "name", "spec", "schema_digest", "type_id"]
_SIGNED_ACTION = ["schema_version", "tenant_id", "timestamp", "action_name", "operation",
                  "subject_refs", "params_digest", "authority", "gate", "action_id"]

def _receipt_digest(doc: dict, fields: list) -> bytes:
    return hashlib.blake2b(canon({k: doc[k] for k in fields}), digest_size=32).digest()

def _result_value(log) -> str:
    r = getattr(log, "result", log)
    return getattr(r, "value", r)

# JSON type-name -> python predicate, for lightweight schema validation
_TYPECHECK = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "ref": lambda v: isinstance(v, str),
}


class WorldModelError(Exception): ...
class ActionDenied(WorldModelError):
    def __init__(self, reason, action_id=None): self.reason = reason; self.action_id = action_id; super().__init__(reason)
class ActionPendingHITL(WorldModelError):
    def __init__(self, action_id): self.action_id = action_id; super().__init__(action_id)


@dataclass
class ActionInvocation:
    action_name: str
    subject_refs: list
    params: dict = field(default_factory=dict)
    authority: dict = field(default_factory=dict)   # {delegation_ref, human_principal, trust_tier}


class WorldModelService:
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
            CREATE TABLE IF NOT EXISTS world_model_types (
              type_id        TEXT PRIMARY KEY,
              tenant_id      TEXT NOT NULL,
              kind           TEXT NOT NULL,        -- object | link | action
              name           TEXT NOT NULL,
              spec           JSONB NOT NULL,
              schema_digest  TEXT NOT NULL,
              document       JSONB,                -- signed type receipt
              signature      TEXT,
              signer_public_key TEXT,
              created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
              UNIQUE (tenant_id, kind, name)
            );
            CREATE TABLE IF NOT EXISTS world_model_actions (
              action_id      TEXT PRIMARY KEY,
              tenant_id      TEXT NOT NULL,
              action_name    TEXT NOT NULL,
              subject_refs   JSONB NOT NULL,
              document       JSONB,                -- signed invocation receipt
              status         TEXT NOT NULL DEFAULT 'invoked',
              signature      TEXT,
              signer_public_key TEXT,
              created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            ALTER TABLE world_model_types   ADD COLUMN IF NOT EXISTS document JSONB;
            ALTER TABLE world_model_actions ADD COLUMN IF NOT EXISTS document JSONB;
            CREATE INDEX IF NOT EXISTS ix_wm_types_tenant   ON world_model_types(tenant_id);
            CREATE INDEX IF NOT EXISTS ix_wm_actions_tenant ON world_model_actions(tenant_id);
            """)

    # ---- type registry ----
    def register_type(self, tenant_id: str, kind: str, name: str, spec: dict) -> dict:
        if kind not in ("object", "link", "action"):
            raise WorldModelError(f"unknown type kind: {kind}")
        self._validate_spec(tenant_id, kind, spec)

        if self.gateway is not None:
            allowed, logs = self.gateway.evaluate_and_gate(
                tenant_id, "worldmodel.type.register", {"kind": kind, "name": name})
            if not allowed:
                raise WorldModelError("type registration denied by governance")

        type_id = compute_type_id(tenant_id, kind, name)
        schema_digest = compute_schema_digest(spec)
        doc = {"schema_version": "wm/0.1", "tenant_id": tenant_id, "timestamp": f"{time.time():.6f}",
               "kind": kind, "name": name, "spec": spec, "schema_digest": schema_digest, "type_id": type_id}
        self._sign_inplace(doc, _SIGNED_TYPE)
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO world_model_types
                (type_id, tenant_id, kind, name, spec, schema_digest, document, signature, signer_public_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (type_id) DO UPDATE SET spec=EXCLUDED.spec, schema_digest=EXCLUDED.schema_digest,
                  document=EXCLUDED.document, signature=EXCLUDED.signature, signer_public_key=EXCLUDED.signer_public_key""",
                (type_id, tenant_id, kind, name, Jsonb(spec), schema_digest, Jsonb(doc),
                 doc.get("signature"), doc.get("signer_public_key")))
        return self.get_type(tenant_id, type_id)

    def get_type(self, tenant_id, type_id) -> dict:
        row = self._get_type(tenant_id, type_id=type_id)
        if not row:
            raise WorldModelError("type not found")
        return row

    def get_type_by_name(self, tenant_id, kind, name) -> Optional[dict]:
        return self._get_type(tenant_id, kind=kind, name=name)

    def list_types(self, tenant_id, kind: Optional[str] = None) -> list:
        with self._get_conn() as conn, conn.cursor() as cur:
            if kind:
                cur.execute("SELECT * FROM world_model_types WHERE tenant_id=%s AND kind=%s ORDER BY name", (tenant_id, kind))
            else:
                cur.execute("SELECT * FROM world_model_types WHERE tenant_id=%s ORDER BY kind, name", (tenant_id,))
            return cur.fetchall()

    def verify_type(self, tenant_id, type_id) -> dict:
        row = self.get_type(tenant_id, type_id)
        doc = row.get("document")
        if not doc:
            return {"passed": False, "checks": {"signature": False, "schema_consistent": False}}
        checks = {"signature": self._verify_sig(doc, _SIGNED_TYPE),
                  "schema_consistent": compute_schema_digest(doc["spec"]) == doc["schema_digest"]}
        return {"passed": all(checks.values()), "checks": checks, "kind": doc["kind"], "name": doc["name"]}

    # ---- validation ----
    def validate_object(self, tenant_id, type_name, instance: dict) -> dict:
        t = self.get_type_by_name(tenant_id, "object", type_name)
        if not t:
            raise WorldModelError(f"object type not found: {type_name}")
        props = (t["spec"] or {}).get("properties", {})
        errors = []
        for pname, pdef in props.items():
            if pname not in instance:
                if pdef.get("required"):
                    errors.append(f"missing required property '{pname}'")
                continue
            check = _TYPECHECK.get(pdef.get("type", "string"), lambda v: True)
            if not check(instance[pname]):
                errors.append(f"property '{pname}' expected {pdef.get('type')}")
        return {"valid": not errors, "errors": errors}

    def validate_link(self, tenant_id, link_name, from_type: str, to_type: str) -> dict:
        t = self.get_type_by_name(tenant_id, "link", link_name)
        if not t:
            raise WorldModelError(f"link type not found: {link_name}")
        spec = t["spec"] or {}
        errors = []
        if spec.get("from_type") != from_type:
            errors.append(f"from_type '{from_type}' != declared '{spec.get('from_type')}'")
        if spec.get("to_type") != to_type:
            errors.append(f"to_type '{to_type}' != declared '{spec.get('to_type')}'")
        return {"valid": not errors, "errors": errors}

    # ---- governed action invocation ----
    def invoke_action(self, tenant_id: str, inv: ActionInvocation) -> dict:
        t = self.get_type_by_name(tenant_id, "action", inv.action_name)
        if not t:
            raise WorldModelError(f"action type not found: {inv.action_name}")
        spec = t["spec"] or {}
        operation = spec.get("operation", f"worldmodel.action.{inv.action_name}")

        # (a) AUTHORITY gate — caller trust tier must meet the action's requirement
        required = spec.get("required_trust_tier", "untrusted")
        caller = (inv.authority or {}).get("trust_tier", "untrusted")
        if _rank(caller) < _rank(required):
            raise ActionDenied(f"insufficient trust tier: '{caller}' < required '{required}'")

        # (b) PARAM validation against the action's input schema
        errs = self._validate_against_schema(spec.get("input_schema", {}), inv.params or {})
        if errs:
            raise WorldModelError("invalid action params: " + "; ".join(errs))

        # (c) GOVERNANCE gate
        if self.gateway is not None:
            allowed, logs = self.gateway.evaluate_and_gate(
                tenant_id, operation, {"action": inv.action_name, "subjects": inv.subject_refs})
            if not allowed:
                ts = f"{time.time():.6f}"
                aid = compute_action_id(tenant_id, inv.action_name, inv.subject_refs, ts)
                if any(_result_value(l) == "escalated" for l in logs):
                    self._persist_action_pending(tenant_id, inv, aid, "waiting_hitl")
                    raise ActionPendingHITL(aid)
                self._persist_action_pending(tenant_id, inv, aid, "denied")
                raise ActionDenied("denied by governance", aid)

        # (d) allowed -> signed, attributable invocation receipt
        return self._emit_receipt(tenant_id, inv, operation)

    def resume_action(self, tenant_id, action_id, approved: bool, approver: str) -> dict:
        row = self.get_action(tenant_id, action_id)
        if row["status"] != "waiting_hitl":
            raise WorldModelError(f"not awaiting HITL (status={row['status']})")
        if not approved:
            self._set_action_status(tenant_id, action_id, "denied")
            return {"action_id": action_id, "status": "denied", "approver": approver}
        inv = ActionInvocation(action_name=row["action_name"], subject_refs=row["subject_refs"])
        t = self.get_type_by_name(tenant_id, "action", inv.action_name)
        operation = (t["spec"] or {}).get("operation", f"worldmodel.action.{inv.action_name}")
        return self._emit_receipt(tenant_id, inv, operation)

    def get_action(self, tenant_id, action_id) -> dict:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM world_model_actions WHERE tenant_id=%s AND action_id=%s", (tenant_id, action_id))
            row = cur.fetchone()
        if not row:
            raise WorldModelError("action not found")
        return row

    def verify_action(self, tenant_id, action_id) -> dict:
        row = self.get_action(tenant_id, action_id)
        doc = row.get("document")
        if not doc:
            return {"passed": False, "checks": {"signature": False}, "status": row.get("status")}
        a = doc.get("authority") or {}
        checks = {"signature": self._verify_sig(doc, _SIGNED_ACTION)}
        recon = {"who": f'{a.get("human_principal")} @ {a.get("trust_tier")}',
                 "did": doc.get("action_name"), "to": doc.get("subject_refs"),
                 "under": a.get("delegation_ref"), "gate": doc.get("gate")}
        return {"passed": all(checks.values()), "checks": checks, "attribution": recon, "status": row.get("status")}

    def get_stats(self, tenant_id) -> dict:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT kind, count(*) AS n FROM world_model_types WHERE tenant_id=%s GROUP BY kind", (tenant_id,))
            types = {r["kind"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT status, count(*) AS n FROM world_model_actions WHERE tenant_id=%s GROUP BY status", (tenant_id,))
            actions = {r["status"]: r["n"] for r in cur.fetchall()}
        return {"types": types, "actions": actions}

    # ---- internals ----
    def _emit_receipt(self, tenant_id, inv: ActionInvocation, operation: str) -> dict:
        ts = f"{time.time():.6f}"
        aid = compute_action_id(tenant_id, inv.action_name, inv.subject_refs, ts)
        doc = {"schema_version": "wm/0.1", "tenant_id": tenant_id, "timestamp": ts,
               "action_name": inv.action_name, "operation": operation, "subject_refs": inv.subject_refs,
               "params_digest": b2_256(canon(inv.params or {})), "authority": inv.authority or {},
               "gate": "allowed", "action_id": aid}
        self._sign_inplace(doc, _SIGNED_ACTION)
        # FUTURE: chain via execution_receipts.issue_receipt(...) — action invocation is step-shaped.
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO world_model_actions
                (action_id, tenant_id, action_name, subject_refs, document, status, signature, signer_public_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (action_id) DO UPDATE SET document=EXCLUDED.document, status=EXCLUDED.status,
                  signature=EXCLUDED.signature, signer_public_key=EXCLUDED.signer_public_key""",
                (aid, tenant_id, inv.action_name, Jsonb(inv.subject_refs), Jsonb(doc), "invoked",
                 doc.get("signature"), doc.get("signer_public_key")))
        return self.get_action(tenant_id, aid)

    def _validate_spec(self, tenant_id, kind, spec) -> None:
        if kind == "object":
            if not isinstance(spec.get("properties"), dict):
                raise WorldModelError("object type requires a 'properties' object")
        elif kind == "link":
            for f in ("from_type", "to_type"):
                if not spec.get(f):
                    raise WorldModelError(f"link type requires '{f}'")
            # referenced object types must already be registered (non-vacuous)
            for ref in (spec["from_type"], spec["to_type"]):
                if not self.get_type_by_name(tenant_id, "object", ref):
                    raise WorldModelError(f"link references unknown object type '{ref}'")
        elif kind == "action":
            if not spec.get("operation"):
                raise WorldModelError("action type requires 'operation'")
            if "required_trust_tier" not in spec:
                raise WorldModelError("action type requires 'required_trust_tier'")

    def _validate_against_schema(self, schema: dict, instance: dict) -> list:
        errors = []
        for pname, pdef in (schema or {}).items():
            if pname not in instance:
                if pdef.get("required"):
                    errors.append(f"missing required '{pname}'")
                continue
            if not _TYPECHECK.get(pdef.get("type", "string"), lambda v: True)(instance[pname]):
                errors.append(f"'{pname}' expected {pdef.get('type')}")
        return errors

    def _sign_inplace(self, doc: dict, fields: list) -> None:
        if not self.signing_key:
            doc["signature"] = None; doc["signer_public_key"] = None; return
        from aml.provenance import sign_provenance
        signature, public_key = sign_provenance(self.signing_key, _receipt_digest(doc, fields))
        doc["signature"] = signature.hex()
        doc["signer_public_key"] = public_key.hex()

    def _verify_sig(self, doc, fields: list) -> bool:
        if not doc.get("signature") or not doc.get("signer_public_key"):
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(doc["signer_public_key"]))
            pub.verify(bytes.fromhex(doc["signature"]), _receipt_digest(doc, fields))
            return True
        except Exception:
            return False

    def _get_type(self, tenant_id, *, type_id=None, kind=None, name=None) -> Optional[dict]:
        with self._get_conn() as conn, conn.cursor() as cur:
            if type_id is not None:
                cur.execute("SELECT * FROM world_model_types WHERE tenant_id=%s AND type_id=%s", (tenant_id, type_id))
            else:
                cur.execute("SELECT * FROM world_model_types WHERE tenant_id=%s AND kind=%s AND name=%s",
                            (tenant_id, kind, name))
            return cur.fetchone()

    def _persist_action_pending(self, tenant_id, inv, action_id, status):
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO world_model_actions (action_id, tenant_id, action_name, subject_refs, status)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (action_id) DO NOTHING""",
                (action_id, tenant_id, inv.action_name, Jsonb(inv.subject_refs), status))

    def _set_action_status(self, tenant_id, action_id, status):
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE world_model_actions SET status=%s WHERE tenant_id=%s AND action_id=%s",
                        (status, tenant_id, action_id))
