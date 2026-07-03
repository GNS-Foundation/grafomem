"""Registry, Selector, Scheduler, Linker, Governance, ExecutionContext, Loader (SPEC-1.0 §2)."""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
from .contracts import feasible, merge, read
from .cso import CSO
from .errors import InfeasibleSchedule, PolicyViolation

import os
import structlog

logger = structlog.get_logger("grafomem")

class Registry:
    def __init__(self, directory=None, trusted_keys=None):
        self._by_id = {}
        self._by_cap = {}
        self._by_model = {}
        self.directory = directory
        self.trusted_keys = trusted_keys or {}
        self.load_errors = []
        if self.directory and os.path.exists(self.directory):
            for fname in os.listdir(self.directory):
                if not fname.endswith(".gfm"): continue
                try:
                    with open(os.path.join(self.directory, fname), "rb") as f:
                        self.register(CSO.from_gfm(f.read(), self.trusted_keys), save=False)
                except ValueError as e:
                    self.load_errors.append((fname, str(e)))

    def register(self, cso: CSO, save=False, private_key=None) -> str:
        sid = cso.content_hash()[:16]
        logger.info("registry.register", sid=sid, model_id=cso.model_id, capabilities=list(cso.capabilities))
        self._by_id[sid] = cso
        if cso.model_id not in self._by_model: self._by_model[cso.model_id] = set()
        self._by_model[cso.model_id].add(sid)
        for cap in cso.capabilities:
            if cap not in self._by_cap: self._by_cap[cap] = set()
            self._by_cap[cap].add(sid)
            
        if save and self.directory and private_key:
            os.makedirs(self.directory, exist_ok=True)
            with open(os.path.join(self.directory, f"{sid}.gfm"), "wb") as f:
                f.write(cso.to_gfm(private_key))
        return sid
        
    def get(self, sid): return self._by_id[sid]
    
    def find(self, capability=None, model_id=None):
        sids = set(self._by_id.keys())
        if capability:
            sids &= self._by_cap.get(capability, set())
        if model_id:
            sids &= self._by_model.get(model_id, set())
        return [(sid, self._by_id[sid]) for sid in sids]

    def check_conformance(self, vocabulary: set[str]):
        """Conformance-test hook for capability vocabulary."""
        invalid = [cap for cap in self._by_cap if cap not in vocabulary]
        if invalid:
            raise ValueError(f"Registry contains non-conformant capabilities: {invalid}")

class Governance:
    def __init__(self, model_id, norm_budget, allowed_consent=("public", "tenant")):
        self.model_id, self.norm_budget, self.allowed = model_id, norm_budget, set(allowed_consent)
    def type_ok(self, c: CSO) -> bool: return c.model_id == self.model_id
    def policy_ok(self, c: CSO) -> bool: return c.consent.get("policy") in self.allowed and c.consent_valid()
    def admissible(self, merged_M, chosen) -> bool:
        return feasible(merged_M, self.norm_budget) and all(self.policy_ok(c) for c in chosen)

class Selector:
    def __init__(self, registry: Registry): self.reg = registry
    def by_registry(self, **meta): return self.reg.find(**meta)
    def by_capability(self, required, model_id):
        req_set = set(required)
        logger.debug("selector.by_capability", required=list(required), model_id=model_id)
        return [(sid, c) for sid, c in self.reg.find(model_id=model_id) if not req_set.isdisjoint(c.capabilities)]
    def behavioral(self, query_distribution):
        raise NotImplementedError("research: behavioral index / quotient metric (RFC0001 §5; Paper D)")


class Scheduler:
    """Greedy set-cover maximizing utility(S) - cost(S) s.t. merge(S) ∈ V ∧ policy(S).
       Coverage is a hard constraint: we must cover `required`, and cost minimizes."""
    def __init__(self, gov: Governance, cost_fn=None, utility_fn=None):
        self.gov = gov
        self.cost_fn = cost_fn if cost_fn else lambda c: 0.0
        self.utility_fn = utility_fn if utility_fn else lambda cov, req: len(cov.intersection(req))

    def schedule(self, candidates, required):
        """Greedy set-cover under the feasibility budget V. Approximate: may raise
        InfeasibleSchedule even when an exact covering subset exists, because greedy can commit
        budget to a high-gain bulky CSO early. An exact solver is a future optimization, not v1.0."""
        req = set(required); chosen, covered = [], set()
        pool = [c for _, c in candidates if self.gov.policy_ok(c)]
        logger.debug("scheduler.start", required=list(req), pool_size=len(pool))
        
        while not req.issubset(covered):
            best, best_score = None, -float('inf')
            for c in pool:
                if c in chosen: continue
                if not feasible(merge(chosen + [c]), self.gov.norm_budget):
                    continue
                
                new_covered = covered | (c.capabilities & req)
                if len(new_covered) == len(covered):
                    continue  # Must provide missing coverage to be considered
                
                utility_gain = self.utility_fn(new_covered, req) - self.utility_fn(covered, req)
                cost = self.cost_fn(c)
                score = utility_gain - cost
                
                if score > best_score:
                    best, best_score = c, score
                    
            if best is None:
                logger.error("scheduler.infeasible", required=list(req - covered), budget=self.gov.norm_budget)
                raise InfeasibleSchedule(f"Infeasible: greedy could not cover {req - covered} within V={self.gov.norm_budget}")
            
            chosen.append(best)
            covered |= (best.capabilities & req)
            
        logger.info("scheduler.complete", chosen_count=len(chosen))
        return chosen

@dataclass
class ExecutionContext:
    model_id: str
    M: "np.ndarray | None" = None
    loaded: list = field(default_factory=list)
    capabilities: set = field(default_factory=set)
    identity: str = "anonymous"
    policy: dict = field(default_factory=dict)
    def run(self, q, act="identity"):
        logger.debug("execution_context.execute", act=act)
        if self.M is None: raise RuntimeError("no state linked")
        return read(self.M, q, act)

class Linker:
    @staticmethod
    def link(ctx: ExecutionContext, states, gov: Governance) -> ExecutionContext:
        logger.debug("linker.link_start", state_count=len(states))
        M = merge(states)
        if not gov.admissible(M, states):
            logger.error("linker.inadmissible")
            raise PolicyViolation("inadmissible link (left V or policy fail)")
        ctx.M = M; ctx.loaded = [c.content_hash()[:16] for c in states]
        ctx.capabilities = set().union(*[c.capabilities for c in states])
        return ctx

@dataclass
class Receipt:
    before: str
    after: str
    scope: str
    key_id: str
    timestamp: str
    signature: bytes

    def verify(self, public_key) -> bool:
        payload = f"{self.before}|{self.after}|{self.scope}|{self.key_id}|{self.timestamp}".encode('utf-8')
        import cryptography.exceptions
        try:
            public_key.verify(self.signature, payload)
            return True
        except cryptography.exceptions.InvalidSignature:
            return False

class Loader:
    """Handles loading .gfm files and instantiating CSOs."""
    @staticmethod
    def load(ctx: ExecutionContext, gfm_bytes: bytes, gov: Governance, trusted_keys: dict):
        c = CSO.from_gfm(gfm_bytes, trusted_keys)
        if not gov.type_ok(c) or not gov.policy_ok(c):
            logger.error("loader.reject_type_or_policy")
            raise PolicyViolation("load rejected (type/policy)")
        logger.info("loader.load", model_id=c.model_id)
        return Linker.link(ctx, [c], gov)
    @staticmethod
    def checkpoint(ctx: ExecutionContext, model_id: str, private_key, key_id: str) -> bytes:
        logger.info("loader.checkpoint", model_id=model_id, key_id=key_id)
        consent = ctx.policy if ctx.policy else {"subject_id": ctx.identity, "policy": "tenant", "expires_at": None}
        return CSO(M=ctx.M, model_id=model_id, capabilities=frozenset(ctx.capabilities), consent=consent, key_id=key_id).to_gfm(private_key)
    @staticmethod
    def erase(ctx: ExecutionContext, scope: str, private_key, key_id: str) -> Receipt:
        logger.info("loader.erase", scope=scope, key_id=key_id)
        import hashlib
        from datetime import datetime, timezone
        if ctx.M is None: raise RuntimeError("no state to erase")
        
        before = hashlib.sha256(ctx.M.tobytes()).hexdigest()
        ctx.M = np.zeros_like(ctx.M)  # v1.0 erase = overwrite; receipt proves the transition
        after = hashlib.sha256(ctx.M.tobytes()).hexdigest()
        timestamp = datetime.now(timezone.utc).isoformat()
        
        payload = f"{before}|{after}|{scope}|{key_id}|{timestamp}".encode('utf-8')
        signature = private_key.sign(payload)
        
        return Receipt(before=before, after=after, scope=scope, key_id=key_id, timestamp=timestamp, signature=signature)
