"""
W6 experiment: deletion / leakage — the two-sided forget primitive.

When the agent is told to forget a fact, two things must hold at once:

  1. LEAKAGE (privacy, false-positive direction): the forgotten fact must not
     resurface. Measured as the fraction of queries whose retrieved set contains
     a fact that was deleted before the query (Check L), concentrated on the
     deleted-probes whose content directly tempts the deleted fact.

  2. OVER-DELETION (correctness, false-negative direction): facts that were NOT
     deleted must remain retrievable. Measured as M1 recall over the
     survivor-probes (non-empty targets).

Four backends span the failure surface:
  - vector_only   : no HARD_DELETE -> deletes no-op -> the content persists (leaks).
  - soft_delete   : claims HARD_DELETE, tombstones, but retrieve ignores the
                    tombstone -> leaks while *claiming* to have deleted.
  - honest_delete : exact removal -> no leak, survivors intact (the correct one).
  - coarse_delete : delete-by-subject -> no leak, but purges same-subject
                    survivors (over-deletes).

A capability *claim* does not certify deletion: soft_delete claims HARD_DELETE
and returns success, yet leaks. Only the benchmark's two-sided check separates
the four. Requires grafomem[backends] (real BGE for the headline numbers).
"""

from __future__ import annotations

from statistics import mean

from aml.backends.delete_backends import (
    CoarseDeleteBackend,
    HonestDeleteBackend,
    SoftDeleteBackend,
)
from aml.backends.vector_only import REFERENCE_MODEL, VectorOnlyBackend
from aml.eval.harness import run_trace
from aml.eval.metrics import _targets_by_turn, bootstrap_paired_ci
from aml.generator.trace import Difficulty
from aml.generator.workloads.w6 import generate_w6

SEEDS = range(5)
BUDGET = 512
DIFF = Difficulty.HARD


def _resolve(embed_fn):
    if embed_fn is not None:
        return embed_fn
    from aml.backends.vector_only import _default_embedder
    print(f"loading {REFERENCE_MODEL} (first run downloads ~130MB) ...")
    return _default_embedder()


def _ts_by_turn(trace):
    return {str(t.turn_id): t.timestamp
            for s in trace.sessions for t in s.turns}


def measure(embed_fn=None, seeds=SEEDS, budget=BUDGET):
    emb = _resolve(embed_fn)
    backends = [
        ("vector_only",   lambda: VectorOnlyBackend(embed_fn=emb)),
        ("soft_delete",   lambda: SoftDeleteBackend(embed_fn=emb)),
        ("honest_delete", lambda: HonestDeleteBackend(embed_fn=emb)),
        ("coarse_delete", lambda: CoarseDeleteBackend(embed_fn=emb)),
    ]
    leak = {n: [] for n, _ in backends}     # per-seed leakage rate
    surv = {n: [] for n, _ in backends}     # per-seed survivor recall
    for s in seeds:
        tr = generate_w6(seed=s, difficulty=DIFF)
        tgt = _targets_by_turn(tr)
        tsb = _ts_by_turn(tr)
        deleted = tr.ground_truth.deleted_facts        # {fid: deleted_at}
        for name, mk in backends:
            run = run_trace(mk(), tr, budget_tokens=budget)
            leak_flags, recalls = [], []
            for qr in run.per_query:
                ts = tsb[qr.turn_id]
                deleted_before = {fid for fid, dt in deleted.items() if dt <= ts}
                leak_flags.append(1.0 if (qr.retrieved & deleted_before) else 0.0)
                T = tgt.get(qr.turn_id, set())
                if T:                                   # survivor-probe
                    recalls.append(len(qr.retrieved & T) / len(T))
            leak[name].append(mean(leak_flags))
            surv[name].append(mean(recalls) if recalls else float("nan"))
    return backends, leak, surv


def _verdict(lk, sv, eps=5e-3):
    leaks = lk > eps
    overdel = sv < 1.0 - eps
    if not leaks and not overdel:
        return "PASS (both)"
    if leaks and overdel:
        return "LEAKS + OVER-DELETES"
    return "LEAKS" if leaks else "OVER-DELETES"


def main(embed_fn=None):
    backends, leak, surv = measure(embed_fn)
    n = len(list(SEEDS))
    print(f"\nW6 deletion / leakage (hard, budget {BUDGET}, {n} seeds):\n")
    print(f"  {'backend':16s} {'leakage rate':>13s} {'survivor recall':>16s}   verdict")
    print("  " + "-" * 72)
    for name, _ in backends:
        lk, sv = mean(leak[name]), mean(surv[name])
        print(f"  {name:16s} {lk:12.3f}  {sv:15.3f}   {_verdict(lk, sv)}")
    print("  " + "-" * 72)

    p, lo, hi = bootstrap_paired_ci(leak["honest_delete"], leak["soft_delete"])
    print(f"\n  leakage   soft - honest:  {p:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]"
          f"  (lo>0 -> soft leaks despite claiming HARD_DELETE)")
    p, lo, hi = bootstrap_paired_ci(surv["coarse_delete"], surv["honest_delete"])
    print(f"  survivor  honest - coarse: {p:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]"
          f"  (lo>0 -> coarse over-deletes survivors)")
    print("\n  Two orthogonal failure directions; only honest_delete passes both.")


if __name__ == "__main__":
    main()
