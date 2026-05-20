#!/usr/bin/env python3
"""
W1 experiment: vector_only (BGE-small) vs persistence floor — M1 / M2 / M3.

Two views:
  1. Per-difficulty at the canonical budget: M1 (recall), M2 (precision),
     M3 (tokens/correct-fact), and the paired bootstrap 95% CI on dM1.
  2. A hard-difficulty budget sweep: how recall, precision, and cost trade off
     as the retrieval budget tightens — the real W1 story, since the headline
     M1 is budget-dependent (target ranks #1 ~80% of the time on hard).

Default uses the pinned BGE-small-en-v1.5 (first run downloads ~130MB).
Pass embed_fn to compare()/sweep_hard() for a stub or controlled rerun.

Usage:  python scripts/run_w1.py
"""

from __future__ import annotations

from statistics import mean, pstdev

from aml.backends.persistence import PersistenceBackend
from aml.backends.vector_only import REFERENCE_MODEL, VectorOnlyBackend
from aml.eval.harness import run_trace
from aml.eval.metrics import bootstrap_paired_ci, score_run
from aml.generator.trace import Difficulty
from aml.generator.workloads.w1 import generate_w1

BUDGET = 512
SEEDS = [0, 1, 2, 3, 4]
DIFFICULTIES = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
SWEEP_BUDGETS = [32, 64, 128, 256, 512]


def _scores(make_backend, diff, seeds, budget):
    rows = []
    for s in seeds:
        tr = generate_w1(seed=s, difficulty=diff)
        rows.append(score_run(run_trace(make_backend(), tr,
                                         budget_tokens=budget), tr))
    return rows


def _resolve_embedder(embed_fn):
    if embed_fn is not None:
        return embed_fn, "injected embedder"
    from aml.backends.vector_only import _default_embedder
    print(f"loading {REFERENCE_MODEL} (first run downloads ~130MB) ...")
    return _default_embedder(), REFERENCE_MODEL


def compare(embed_fn=None, seeds=SEEDS, difficulties=DIFFICULTIES,
            budget=BUDGET):
    embed_fn, label = _resolve_embedder(embed_fn)

    print(f"\nW1: vector_only [{label}] vs persistence floor")
    print(f"budget = {budget} chars   |   seeds = {seeds}\n")
    print(f"  {'diff':7s} {'backend':8s}  {'M1':14s} {'M2':8s} {'M3 (char/fact)':16s}")
    print("  " + "-" * 58)

    for diff in difficulties:
        floor = _scores(lambda: PersistenceBackend(), diff, seeds, budget)
        vec = _scores(lambda: VectorOnlyBackend(embed_fn=embed_fn),
                      diff, seeds, budget)
        point, lo, hi = bootstrap_paired_ci(
            [r["m1"] for r in floor], [r["m1"] for r in vec])

        def row(name, rows):
            m1 = mean(r["m1"] for r in rows)
            sd = pstdev(r["m1"] for r in rows)
            m2 = mean(r["m2"] for r in rows)
            m3 = mean(r["m3"] for r in rows)
            print(f"  {diff.value:7s} {name:8s}  {m1:5.3f}+/-{sd:5.3f}  "
                  f"{m2:6.3f}  {m3:14.1f}")

        row("floor", floor)
        row("vector", vec)
        verdict = "beats floor" if lo > 0 else "inconclusive"
        print(f"          dM1 = {point:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]"
              f"  -> {verdict}")
        print("  " + "-" * 58)
    return embed_fn


def sweep_hard(embed_fn, seeds=SEEDS, budgets=SWEEP_BUDGETS):
    print("\nHard-difficulty budget sweep (vector_only) — recall/precision/cost:\n")
    print(f"  {'budget':7s}  {'~facts':7s}  {'M1':8s} {'M2':8s} "
          f"{'M3 (char/fact)':16s}")
    print("  " + "-" * 52)
    for B in budgets:
        rows = _scores(lambda: VectorOnlyBackend(embed_fn=embed_fn),
                       Difficulty.HARD, seeds, B)
        m1 = mean(r["m1"] for r in rows)
        m2 = mean(r["m2"] for r in rows)
        m3 = mean(r["m3"] for r in rows)
        print(f"  {B:6d}   {B/26:6.0f}   {m1:6.3f}  {m2:6.3f}  {m3:14.1f}")
    print("  " + "-" * 52)
    print("  (W1-hard facts avg ~26 chars; M3 lower = cheaper per correct fact)")


if __name__ == "__main__":
    emb = compare()
    sweep_hard(emb)
    raise SystemExit(0)
