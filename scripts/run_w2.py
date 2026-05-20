#!/usr/bin/env python3
"""
W2 experiment: floor / vector_only / supersession_chain / bi_temporal on Drift,
scored with CURRENT and HISTORICAL queries reported SEPARATELY.

  - CURRENT  (as_of=None): target = chain head. Scored for all four backends.
  - HISTORICAL (as_of=t):  target = the version valid at t. N/A for every
                           non-BI_TEMPORAL backend (harness skips, E1); scored
                           only for bi_temporal.

The story in one table: vector_only and supersession_chain use the SAME BGE
embedder as bi_temporal; the only differences are capabilities. supersession
recovers tight-budget CURRENT recall (F4); bi_temporal additionally answers the
HISTORICAL queries that are unscored for everyone else (F5) — the second axis.

Usage:  python scripts/run_w2.py
"""

from __future__ import annotations

from statistics import mean, pstdev

from aml.backends.bi_temporal import BiTemporalVectorBackend
from aml.backends.persistence import PersistenceBackend
from aml.backends.supersession_chain import SupersessionVectorBackend
from aml.backends.vector_only import REFERENCE_MODEL, VectorOnlyBackend
from aml.eval.harness import run_trace
from aml.eval.metrics import _targets_by_turn
from aml.generator.trace import Difficulty, TurnRole
from aml.generator.workloads.w2 import generate_w2

BUDGET = 512
SEEDS = [0, 1, 2, 3, 4]
DIFFICULTIES = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
SWEEP_BUDGETS = [32, 64, 128, 256, 512]


def _is_historical(trace) -> dict[str, bool]:
    out = {}
    for s in trace.sessions:
        for t in s.turns:
            if t.role == TurnRole.AGENT_QUERY:
                out[str(t.turn_id)] = t.as_of is not None
    return out


def m1_split(run, trace):
    """(current_m1, historical_m1|nan, n_current, n_historical) for one run."""
    kinds = _is_historical(trace)
    tgt = _targets_by_turn(trace)
    cur, hist = [], []
    for qr in run.per_query:
        T = tgt.get(qr.turn_id, set())
        if not T:
            continue
        r = len(qr.retrieved & T) / len(T)
        (hist if kinds.get(qr.turn_id) else cur).append(r)
    cm = mean(cur) if cur else float("nan")
    hm = mean(hist) if hist else float("nan")
    return cm, hm, len(cur), len(hist)


def _split_runs(make_backend, diff, seeds, budget):
    cur, hist, n_hist = [], [], 0
    for s in seeds:
        tr = generate_w2(seed=s, difficulty=diff)
        run = run_trace(make_backend(), tr, budget_tokens=budget)
        cm, hm, _nc, nh = m1_split(run, tr)
        cur.append(cm)
        if nh:
            hist.append(hm)
            n_hist = nh
    return cur, hist, n_hist


def _resolve(embed_fn):
    if embed_fn is not None:
        return embed_fn, "injected embedder"
    from aml.backends.vector_only import _default_embedder
    print(f"loading {REFERENCE_MODEL} (first run downloads ~130MB) ...")
    return _default_embedder(), REFERENCE_MODEL


def _fmt(vals):
    return f"{mean(vals):5.3f}+/-{pstdev(vals):5.3f}"


def compare(embed_fn=None, seeds=SEEDS, difficulties=DIFFICULTIES, budget=BUDGET):
    embed_fn, label = _resolve(embed_fn)

    print(f"\nW2 (Drift): four backends, CURRENT vs HISTORICAL M1 [{label}]")
    print(f"budget = {budget} chars   |   seeds = {seeds}\n")
    print(f"  {'diff':7s} {'backend':14s}  {'current M1':14s} {'historical M1':14s}")
    print("  " + "-" * 56)

    builders = [
        ("floor", lambda: PersistenceBackend()),
        ("vector_only", lambda: VectorOnlyBackend(embed_fn=embed_fn)),
        ("supersession", lambda: SupersessionVectorBackend(embed_fn=embed_fn)),
        ("bi_temporal", lambda: BiTemporalVectorBackend(embed_fn=embed_fn)),
    ]
    for diff in difficulties:
        n_hist_seen = 0
        for name, mk in builders:
            cur, hist, n_hist = _split_runs(mk, diff, seeds, budget)
            hist_col = _fmt(hist) if hist else "N/A"
            if hist:
                n_hist_seen = n_hist
            print(f"  {diff.value:7s} {name:14s}  {_fmt(cur):14s} {hist_col:14s}")
        print(f"          historical queries answerable only by bi_temporal "
              f"({n_hist_seen} per seed)")
        print("  " + "-" * 56)
    return embed_fn


def sweep_current(embed_fn, seeds=SEEDS, budgets=SWEEP_BUDGETS):
    print("\nHard CURRENT-query sweep — vector vs supersession vs bi_temporal:\n")
    print(f"  {'budget':7s}  {'vector':9s} {'supersess':10s} {'bi_temp':9s}")
    print("  " + "-" * 40)
    for B in budgets:
        v, _, _ = _split_runs(lambda: VectorOnlyBackend(embed_fn=embed_fn),
                              Difficulty.HARD, seeds, B)
        s, _, _ = _split_runs(lambda: SupersessionVectorBackend(embed_fn=embed_fn),
                              Difficulty.HARD, seeds, B)
        t, _, _ = _split_runs(lambda: BiTemporalVectorBackend(embed_fn=embed_fn),
                              Difficulty.HARD, seeds, B)
        print(f"  {B:6d}   {mean(v):7.3f}  {mean(s):8.3f}  {mean(t):7.3f}")
    print("  " + "-" * 40)
    print("  (bi_temporal == supersession on current: identical head retrieval)")


def sweep_historical(embed_fn, seeds=SEEDS, budgets=SWEEP_BUDGETS):
    print("\nHard HISTORICAL-query sweep — bi_temporal (N/A for all others):\n")
    print(f"  {'budget':7s}  {'historical M1':14s}")
    print("  " + "-" * 26)
    for B in budgets:
        _, h, _ = _split_runs(lambda: BiTemporalVectorBackend(embed_fn=embed_fn),
                              Difficulty.HARD, seeds, B)
        print(f"  {B:6d}   {mean(h):12.3f}")
    print("  " + "-" * 26)
    print("  (as_of slices to one version/chain -> W1-like frontier in the past)")


if __name__ == "__main__":
    emb = compare()
    sweep_current(emb)
    sweep_historical(emb)
    raise SystemExit(0)
