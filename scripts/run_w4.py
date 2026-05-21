#!/usr/bin/env python3
"""
W4 experiment: long-horizon dependencies — the recall-vs-footprint tradeoff.

Every fact is a distinct entity, so retrieval is unambiguous; the only question
is operational. Two views:

  1. Recall binned by distance d (= facts introduced after the target). The
     unbounded vector store is flat (it kept everything); bounded_vector(K)
     cliffs at d=K (evicted); the recency floor cliffs even earlier, at roughly
     budget/fact-size (it never looks at the query, just returns recent turns).
     The cliff is structural — independent of the embedder.

  2. Footprint across horizon tiers. Read straight from audit(): the retained
     memory count, which for a store-and-scan backend is BOTH the storage cost
     (M5) and the per-query scan cost (M4). Unbounded grows linearly with the
     horizon; bounded plateaus at K. One deterministic number, no wall-clock.

The tradeoff: unbounded pays linear cost for flat recall; bounded pays a recall
cliff beyond K for flat, horizon-independent cost.

Usage:  python scripts/run_w4.py
"""

from __future__ import annotations

from statistics import mean

from aml.backends.bounded_vector import BoundedVectorBackend
from aml.backends.persistence import PersistenceBackend
from aml.backends.vector_only import REFERENCE_MODEL, VectorOnlyBackend, _stub_embedder
from aml.eval.harness import run_trace
from aml.eval.metrics import _targets_by_turn
from aml.generator.trace import Difficulty, TurnRole
from aml.generator.workloads.w4 import generate_w4

K = 64
BUDGET = 512
SEEDS = [0, 1, 2, 3, 4]
TIERS = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
BINS = [(1, 16), (16, 64), (64, 256), (256, 1024), (1024, 10**9)]


def _distance_of(trace) -> dict[str, int]:
    """turn_id (query) -> distance d = facts introduced after the target."""
    intro_ts = {}
    for s in trace.sessions:
        for t in s.turns:
            for fid in t.introduces:
                intro_ts[fid] = t.timestamp
    n = len(trace.facts)
    base = min(intro_ts.values())
    out = {}
    for s in trace.sessions:
        for t in s.turns:
            if t.role == TurnRole.AGENT_QUERY:
                fid = t.requires[0]
                i = round((intro_ts[fid] - base).total_seconds())
                out[str(t.turn_id)] = n - 1 - i
    return out


def _bin_label(lo, hi):
    return f"<{hi}" if lo < 16 else (f">={lo}" if hi > 10**8 else f"{lo}-{hi-1}")


def _resolve(embed_fn):
    if embed_fn is not None:
        return embed_fn, "injected"
    from aml.backends.vector_only import _default_embedder
    print(f"loading {REFERENCE_MODEL} (first run downloads ~130MB) ...")
    return _default_embedder(), REFERENCE_MODEL


def recall_by_distance(embed_fn=None, seeds=SEEDS, k=K):
    embed_fn, label = _resolve(embed_fn)
    backends = [
        ("vector_only", lambda: VectorOnlyBackend(embed_fn=embed_fn)),
        (f"bounded(K={k})", lambda: BoundedVectorBackend(capacity=k, embed_fn=embed_fn)),
        ("floor", lambda: PersistenceBackend()),
    ]
    # bin -> backend -> list of per-query recalls
    agg = {b: {name: [] for name, _ in backends} for b in BINS}
    for s in seeds:
        tr = generate_w4(seed=s, difficulty=Difficulty.HARD)
        dmap = _distance_of(tr)
        tgt = _targets_by_turn(tr)
        for name, mk in backends:
            run = run_trace(mk(), tr, budget_tokens=BUDGET)
            for qr in run.per_query:
                d = dmap.get(qr.turn_id)
                T = tgt.get(qr.turn_id, set())
                if d is None or not T:
                    continue
                r = len(qr.retrieved & T) / len(T)
                for b in BINS:
                    if b[0] <= d < b[1]:
                        agg[b][name].append(r)
                        break

    print(f"\nW4 recall by distance d (hard, budget {BUDGET}) [{label}]:\n")
    hdr = "  " + f"{'distance':10s}" + "".join(f"{n:>15s}" for n, _ in backends)
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for b in BINS:
        row = f"  {_bin_label(*b):10s}"
        for name, _ in backends:
            vals = agg[b][name]
            row += f"{(mean(vals) if vals else float('nan')):>15.3f}"
        print(row)
    print("  " + "-" * (len(hdr) - 2))
    print(f"  (bounded cliffs at d=K={k}; floor at ~budget/fact-size; vector flat)")
    return embed_fn


def footprint(embed_fn, seeds=SEEDS, k=K, tiers=TIERS):
    print("\nFootprint = retained memories = scan cost (M5 == M4), from audit():\n")
    print(f"  {'tier':7s} {'horizon':8s}  {'vector retain':14s} {'bounded retain':15s}  "
          f"{'vec recall':11s} {'bnd recall':10s}")
    print("  " + "-" * 74)
    for diff in tiers:
        vr, br, vrec, brec, horizon = [], [], [], [], 0
        for s in seeds:
            tr = generate_w4(seed=s, difficulty=diff)
            horizon = len(tr.facts)
            tgt = _targets_by_turn(tr)
            for mk, retain, rec in (
                (lambda: VectorOnlyBackend(embed_fn=embed_fn), vr, vrec),
                (lambda: BoundedVectorBackend(capacity=k, embed_fn=embed_fn), br, brec),
            ):
                b = mk()
                run = run_trace(b, tr, budget_tokens=BUDGET)
                retain.append(len(list(b.audit())))
                rec.append(mean(len(qr.retrieved & tgt[qr.turn_id]) / len(tgt[qr.turn_id])
                                for qr in run.per_query if tgt.get(qr.turn_id)))
        print(f"  {diff.value:7s} {horizon:7d}   {mean(vr):12.0f}   {mean(br):13.0f}    "
              f"{mean(vrec):9.3f}   {mean(brec):8.3f}")
    print("  " + "-" * 74)
    print(f"  (unbounded retain grows with horizon; bounded plateaus at K={k})")


if __name__ == "__main__":
    emb = recall_by_distance()
    footprint(emb)
    raise SystemExit(0)
