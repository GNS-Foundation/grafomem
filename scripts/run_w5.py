"""
W5 experiment: tenant isolation — the second half of the privacy axis.

When facts from many tenants share one store, two things must hold at once for a
query issued by tenant T:

  1. LEAKAGE (privacy, false-positive direction): no fact owned by another
     tenant may be returned. Measured (M7) as the fraction of queries whose
     retrieved set contains a fact whose owning tenant != the querying tenant.
     W5 makes this maximally tempting: every subject exists under every tenant
     with a different object, so a tenant's query is byte-identical to another
     tenant's and a tenant-blind store ranks the wrong tenant's fact at the top.

  2. IN-TENANT RECALL (correctness, false-negative direction): the tenant's own
     facts must remain retrievable. Measured as M1 recall over the in-tenant
     probes (each targets its own tenant's single fact).

Four backends span the failure surface:
  - vector_only   : no MULTI_TENANT -> harness passes tenant_id=None -> one pool
                    -> ranks every tenant's fact -> leaks.
  - leaky_tenant  : claims MULTI_TENANT, tags each fact's tenant, but retrieve
                    ignores the querying tenant -> leaks while *claiming*
                    isolation (the tenancy analog of soft_delete).
  - tenant_scoped : retrieve ranks only the querying tenant's facts -> no leak,
                    own facts intact (the correct one).
  - over_isolating: tenant-scoped but withholds any subject shared across
                    tenants ("ambiguous -> might leak") -> no leak, but drops
                    its own facts (over-isolates; the analog of coarse_delete).

A capability *claim* does not certify isolation: leaky_tenant claims
MULTI_TENANT and accepts every tenant_id, yet leaks. Only the benchmark's
two-sided check separates the four. Requires grafomem[backends] (real BGE).
"""

from __future__ import annotations

from statistics import mean

from aml.backends.tenant_backends import (
    LeakyTenant,
    OverIsolating,
    TenantScoped,
)
from aml.backends.vector_only import REFERENCE_MODEL, VectorOnlyBackend
from aml.eval.harness import run_trace
from aml.eval.metrics import _targets_by_turn, bootstrap_paired_ci
from aml.generator.trace import Difficulty
from aml.generator.workloads.w5 import generate_w5

SEEDS = range(5)
BUDGET = 512
DIFF = Difficulty.HARD


def _resolve(embed_fn):
    if embed_fn is not None:
        return embed_fn
    from aml.backends.vector_only import _default_embedder
    print(f"loading {REFERENCE_MODEL} (first run downloads ~130MB) ...")
    return _default_embedder()


def _tenant_by_turn(trace):
    """str(turn_id) -> the tenant of the session that issued it."""
    return {str(t.turn_id): s.tenant_id
            for s in trace.sessions for t in s.turns}


def measure(embed_fn=None, seeds=SEEDS, budget=BUDGET):
    emb = _resolve(embed_fn)
    backends = [
        ("vector_only",    lambda: VectorOnlyBackend(embed_fn=emb)),
        ("leaky_tenant",   lambda: LeakyTenant(embed_fn=emb)),
        ("tenant_scoped",  lambda: TenantScoped(embed_fn=emb)),
        ("over_isolating", lambda: OverIsolating(embed_fn=emb)),
    ]
    leak = {n: [] for n, _ in backends}     # per-seed cross-tenant leakage rate
    rec = {n: [] for n, _ in backends}      # per-seed in-tenant recall
    for s in seeds:
        tr = generate_w5(seed=s, difficulty=DIFF)
        tgt = _targets_by_turn(tr)
        q_tenant = _tenant_by_turn(tr)
        fid_tenant = {f.fact_id: f.tenant_id for f in tr.facts}
        for name, mk in backends:
            run = run_trace(mk(), tr, budget_tokens=budget)
            leak_flags, recalls = [], []
            for qr in run.per_query:
                qt = q_tenant[qr.turn_id]
                cross = any(fid_tenant.get(fid) != qt for fid in qr.retrieved)
                leak_flags.append(1.0 if cross else 0.0)
                T = tgt.get(qr.turn_id, set())
                if T:                                   # in-tenant probe
                    recalls.append(len(qr.retrieved & T) / len(T))
            leak[name].append(mean(leak_flags))
            rec[name].append(mean(recalls) if recalls else float("nan"))
    return backends, leak, rec


def _verdict(lk, rc, eps=5e-3):
    leaks = lk > eps
    under = rc < 1.0 - eps
    if not leaks and not under:
        return "PASS (both)"
    if leaks and under:
        return "LEAKS + OVER-ISOLATES"
    return "LEAKS" if leaks else "OVER-ISOLATES"


def main(embed_fn=None):
    backends, leak, rec = measure(embed_fn)
    n = len(list(SEEDS))
    print(f"\nW5 tenant isolation (hard, budget {BUDGET}, {n} seeds):\n")
    print(f"  {'backend':16s} {'leakage rate':>13s} {'in-tenant recall':>17s}   verdict")
    print("  " + "-" * 74)
    for name, _ in backends:
        lk, rc = mean(leak[name]), mean(rec[name])
        print(f"  {name:16s} {lk:12.3f}  {rc:16.3f}   {_verdict(lk, rc)}")
    print("  " + "-" * 74)

    p, lo, hi = bootstrap_paired_ci(leak["tenant_scoped"], leak["leaky_tenant"])
    print(f"\n  leakage          leaky - scoped: {p:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]"
          f"  (lo>0 -> leaky leaks despite claiming MULTI_TENANT)")
    p, lo, hi = bootstrap_paired_ci(rec["over_isolating"], rec["tenant_scoped"])
    print(f"  in-tenant recall scoped - over:  {p:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]"
          f"  (lo>0 -> over_isolating drops its own tenant's facts)")
    print("\n  Two orthogonal failure directions; only tenant_scoped passes both.")


if __name__ == "__main__":
    main()
