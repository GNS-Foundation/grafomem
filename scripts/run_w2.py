#!/usr/bin/env python3
"""
W2 experiment: vector_only (BGE-small) vs persistence floor on Drift & Conflict.

Neither backend claims BI_TEMPORAL, so historical (as_of) queries are N/A for
both and excluded from Q_W (E1) — reported as the n_na count. Scoring is over
CURRENT queries: "what is S's P now?", whose target is the chain head.

The W2 story is in the budget sweep. A backend without SUPERSESSION_CHAIN keeps
every version ("Rome" and "Milan"), and cosine cannot tell which is current —
both match "where does S live?" equally. At a tight budget the backend returns
the wrong (superseded) version about as often as the right one, so current-query
recall drops toward chance. A supersession backend would keep only the head and
score ~1.0 — that gap is what W2 is built to expose.

Default uses pinned BGE-small-en-v1.5. Pass embed_fn for a stub/controlled rerun.

Usage:  python scripts/run_w2.py
"""

from __future__ import annotations

from statistics import mean, pstdev

from aml.backends.persistence import PersistenceBackend
from aml.backends.vector_only import REFERENCE_MODEL, VectorOnlyBackend
from aml.eval.harness import run_trace
from aml.eval.metrics import bootstrap_paired_ci, score_run
from aml.generator.trace import Difficulty
from aml.generator.workloads.w2 import generate_w2

BUDGET = 512
SEEDS = [0, 1, 2, 3, 4]
DIFFICULTIES = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
SWEEP_BUDGETS = [32, 64, 128, 256, 512]


def _runs(make_backend, diff, seeds, budget):
    out = []
    for s in seeds:
        tr = generate_w2(seed=s, difficulty=diff)
        run = run_trace(make_backend(), tr, budget_tokens=budget)
        out.append((score_run(run, tr), run.n_na))
    return out


def _resolve(embed_fn):
    if embed_fn is not None:
        return embed_fn, "injected embedder"
    from aml.backends.vector_only import _default_embedder
    print(f"loading {REFERENCE_MODEL} (first run downloads ~130MB) ...")
    return _default_embedder(), REFERENCE_MODEL


def compare(embed_fn=None, seeds=SEEDS, difficulties=DIFFICULTIES, budget=BUDGET):
    embed_fn, label = _resolve(embed_fn)

    print(f"\nW2 (Drift): vector_only [{label}] vs persistence floor")
    print(f"budget = {budget} chars   |   seeds = {seeds}   |   "
          f"scoring CURRENT queries (historical = N/A, no BI_TEMPORAL)\n")
    print(f"  {'diff':7s} {'backend':8s}  {'M1':14s} {'M2':8s} {'M3':10s} {'N/A':5s}")
    print("  " + "-" * 56)

    for diff in difficulties:
        floor = _runs(lambda: PersistenceBackend(), diff, seeds, budget)
        vec = _runs(lambda: VectorOnlyBackend(embed_fn=embed_fn),
                    diff, seeds, budget)
        point, lo, hi = bootstrap_paired_ci(
            [s["m1"] for s, _ in floor], [s["m1"] for s, _ in vec])

        def row(name, rows):
            m1 = mean(s["m1"] for s, _ in rows)
            sd = pstdev(s["m1"] for s, _ in rows)
            m2 = mean(s["m2"] for s, _ in rows)
            m3 = mean(s["m3"] for s, _ in rows)
            na = rows[0][1]
            print(f"  {diff.value:7s} {name:8s}  {m1:5.3f}+/-{sd:5.3f}  "
                  f"{m2:6.3f}  {m3:8.1f}  {na:4d}")

        row("floor", floor)
        row("vector", vec)
        verdict = "beats floor" if lo > 0 else "inconclusive"
        print(f"          dM1 = {point:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]"
              f"  -> {verdict}")
        print("  " + "-" * 56)
    return embed_fn


def sweep_hard(embed_fn, seeds=SEEDS, budgets=SWEEP_BUDGETS):
    print("\nHard-difficulty budget sweep (vector_only, CURRENT queries):\n")
    print(f"  {'budget':7s}  {'M1':8s} {'M2':8s} {'M3':10s}")
    print("  " + "-" * 38)
    for B in budgets:
        rows = _runs(lambda: VectorOnlyBackend(embed_fn=embed_fn),
                     Difficulty.HARD, seeds, B)
        m1 = mean(s["m1"] for s, _ in rows)
        m2 = mean(s["m2"] for s, _ in rows)
        m3 = mean(s["m3"] for s, _ in rows)
        print(f"  {B:6d}   {m1:6.3f}  {m2:6.3f}  {m3:8.1f}")
    print("  " + "-" * 38)
    print("  (tight budget: can it pick the CURRENT version over superseded ones?)")


if __name__ == "__main__":
    emb = compare()
    sweep_hard(emb)
    raise SystemExit(0)
