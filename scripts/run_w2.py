#!/usr/bin/env python3
"""
W2 experiment: persistence floor / vector_only / supersession_chain on Drift.

All three are scored on CURRENT queries ("what is S's P now?", target = head).
Historical (as_of) queries are N/A for all three (none claim BI_TEMPORAL) and
excluded from Q_W (E1) — reported as n_na.

The point of the three-way: vector_only and supersession_chain use the SAME
BGE-small embedder. The only difference is the capability — supersession_chain
calls supersede() to retire stale versions, so a current query sees one
candidate per chain instead of d. If the tight-budget recall recovers from
~1/depth toward ~1.0, that gain is attributable to the capability, not the
model. That is the whole capability-gated thesis in one table.

Default uses pinned BGE-small-en-v1.5; pass embed_fn for a stub/controlled run.

Usage:  python scripts/run_w2.py
"""

from __future__ import annotations

from statistics import mean, pstdev

from aml.backends.persistence import PersistenceBackend
from aml.backends.supersession_chain import SupersessionVectorBackend
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

    print(f"\nW2 (Drift): floor / vector_only / supersession_chain [{label}]")
    print(f"budget = {budget} chars   |   seeds = {seeds}   |   "
          f"scoring CURRENT queries (historical = N/A)\n")
    print(f"  {'diff':7s} {'backend':14s}  {'M1':14s} {'M2':8s} {'M3':9s} {'N/A':5s}")
    print("  " + "-" * 62)

    def _agg(rows, key):
        return mean(s[key] for s, _ in rows), pstdev(s[key] for s, _ in rows)

    for diff in difficulties:
        floor = _runs(lambda: PersistenceBackend(), diff, seeds, budget)
        vec = _runs(lambda: VectorOnlyBackend(embed_fn=embed_fn), diff, seeds, budget)
        sup = _runs(lambda: SupersessionVectorBackend(embed_fn=embed_fn),
                    diff, seeds, budget)

        for name, rows in (("floor", floor), ("vector_only", vec),
                           ("supersession", sup)):
            m1, sd = _agg(rows, "m1")
            m2, _ = _agg(rows, "m2")
            m3, _ = _agg(rows, "m3")
            print(f"  {diff.value:7s} {name:14s}  {m1:5.3f}+/-{sd:5.3f}  "
                  f"{m2:6.3f}  {m3:7.1f}  {rows[0][1]:4d}")

        # Headline: the capability gain (supersession vs vector, same embedder).
        point, lo, hi = bootstrap_paired_ci(
            [s["m1"] for s, _ in vec], [s["m1"] for s, _ in sup])
        verdict = "capability gain" if lo > 0 else "no gain"
        print(f"          dM1(supersession - vector) = {point:+.3f}  "
              f"95% CI [{lo:+.3f}, {hi:+.3f}]  -> {verdict}")
        print("  " + "-" * 62)
    return embed_fn


def sweep_hard(embed_fn, seeds=SEEDS, budgets=SWEEP_BUDGETS):
    print("\nHard budget sweep — vector_only vs supersession_chain (CURRENT M1):\n")
    print(f"  {'budget':7s}  {'vector M1':11s} {'supersession M1':16s} {'gain':6s}")
    print("  " + "-" * 46)
    for B in budgets:
        vec = _runs(lambda: VectorOnlyBackend(embed_fn=embed_fn),
                    Difficulty.HARD, seeds, B)
        sup = _runs(lambda: SupersessionVectorBackend(embed_fn=embed_fn),
                    Difficulty.HARD, seeds, B)
        vm = mean(s["m1"] for s, _ in vec)
        sm = mean(s["m1"] for s, _ in sup)
        print(f"  {B:6d}   {vm:9.3f}   {sm:14.3f}   {sm - vm:+.3f}")
    print("  " + "-" * 46)
    print("  (tight budgets: supersession leaves ONE current candidate per chain)")


if __name__ == "__main__":
    emb = compare()
    sweep_hard(emb)
    raise SystemExit(0)
