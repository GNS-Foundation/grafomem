"""R5 — the governed world-model interface (ontology-agnostic but ontology-governed).

Object / Link / Action types over the GMP fact substrate. The differentiator is the
crypto-governed write-path: every Action requires a GEIANT delegation at a sufficient
TierGate level, is gated by policy, and is recorded in gcrumbs.

Phase B: becomes src/aml/cloud/world_model.py (+ world_model_routes.py), with the
action gate delegated to cloud/policy_engine.py (PDP) + cloud/governance.py (PEP).
"""
import hashlib
from .hashing import b2_128, US
from .identity import tier_rank, verify_delegation


class WorldModel:
    def __init__(self, crumbs):
        self.crumbs = crumbs
        self.object_types, self.link_types, self.action_types = {}, {}, {}
        self.objects, self.links = {}, []

    # ---- declarations ----
    def declare_object_type(self, name, properties, identity_props, maps_from="fact"):
        self.object_types[name] = {"name": name, "properties": properties,
                                   "identity": identity_props, "maps_from": maps_from}

    def declare_link_type(self, name, frm, to, cardinality):
        self.link_types[name] = {"name": name, "from_type": frm, "to_type": to, "cardinality": cardinality}

    def declare_action_type(self, name, effect, targets, min_tier,
                            requires_deleg=True, hitl=False, gateway_policy="default-allow"):
        self.action_types[name] = {"name": name, "effect": effect, "targets": targets,
                                   "authority": {"min_tier": min_tier, "requires_deleg": requires_deleg, "hitl": hitl},
                                   "gateway_policy": gateway_policy, "recording": "gcrumbs"}

    # ---- instances ----
    def add_object(self, otype, props):
        ident = US.join(str(props[p]).encode() for p in self.object_types[otype]["identity"])
        oid = b2_128(otype, hashlib.blake2b(ident, digest_size=16).hexdigest())
        self.objects[oid] = {"obj_id": oid, "type": otype, "props": props}
        return oid

    def add_link(self, ltype, frm_id, to_id):
        self.links.append({"link_type": ltype, "from": frm_id, "to": to_id})

    # ---- the crypto-governed write-path (the R5 differentiator) ----
    def execute_action(self, action_name, args, delegation, presented_tier,
                       gateway_allow=True, hitl_approved=False):
        """Returns (authorized, reasons). Authorized AND denied are both recorded."""
        auth = self.action_types[action_name]["authority"]
        reasons = []
        if auth["requires_deleg"] and not (delegation and verify_delegation(delegation)):
            reasons.append("no/invalid GEIANT delegation")
        if tier_rank(presented_tier) < tier_rank(auth["min_tier"]):
            reasons.append(f"tier {presented_tier} < required {auth['min_tier']}")
        if not gateway_allow:
            reasons.append("governance gateway denied")
        if auth["hitl"] and not hitl_approved:
            reasons.append("HITL approval missing")
        authorized = not reasons
        self.crumbs.emit(f"action:{action_name}:{'ok' if authorized else 'deny'}",
                         {"args": args, "authorized": authorized, "reasons": reasons,
                          "agent": delegation.get("agent_handle") if delegation else None,
                          "tier": presented_tier})
        return authorized, reasons

    # ---- conformance: G3 structural validity ----
    def validate(self):
        errs = []
        declared = set(self.object_types)
        for oid, o in self.objects.items():
            if o["type"] not in self.object_types:
                errs.append(f"untyped object {oid}")
        for lk in self.links:
            if lk["link_type"] not in self.link_types:
                errs.append(f"undeclared link type {lk['link_type']}"); continue
            if self.objects.get(lk["from"], {}).get("type") not in declared:
                errs.append(f"dangling link from {lk['from']}")
            if self.objects.get(lk["to"], {}).get("type") not in declared:
                errs.append(f"dangling link to {lk['to']}")
        for an, at in self.action_types.items():
            if at["recording"] != "gcrumbs" or "min_tier" not in at["authority"]:
                errs.append(f"ungoverned action {an}")
        return errs
