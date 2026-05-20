#!/usr/bin/env python3
"""
W3 experiment: precision under distractor noise — the boundary probe.

W1 and W2 were structural failure modes: a capability (supersession, valid-time)
reshaped the candidate set so even a weak lexical embedder kept pace. W3 has no
structure to lean on — signal and near-miss distractors share subject and object
type, differing only in the predicate — so the task is pure semantic
discrimination. This is where embedding QUALITY should finally matter.

Two backends use vector_only with different embedders, holding everything else
fixed:
  - BGE  : the pinned reference embedder (semantic).
  - stub : a bag-of-words lexical baseline. It overlaps signal and near-miss on
           the subject token alone (can't tell "lives" from "born"), so it
           should collapse toward 1/(1+near-misses) — the discrimination floor.

Metrics: M2 precision (headline — how much of what you return is signal) and
recall at a tight budget (discrimination — does the signal outrank its
near-misses?). floor (recency) is the precision disaster baseline.

Usage:  python scripts/run_w3.py
"""

from __future__ import annotations

from statistics import mean, pstdev

from aml.backends.bi_temporal import BiTemporalVectorBackend
from aml.backends.persistence import PersistenceBackend
from aml.backends.supersession_chain import SupersessionVectorBackend
from aml.backends.vector_only import REFERENCE_MODEL, VectorOnlyBackend, _stub_embedder
from aml.eval.harness import run_trace
from aml.eval.metrics import score_run
from aml.generator.trace import Difficulty
from aml.generator.workloads.w1 import generate_w1
from aml.generator.workloads.w2 import generate_w2
from aml.generator.workloads.w3 import generate_w3

BUDGET = 512
SEEDS = [0, 1, 2, 3, 4]
DIFFICULTIES = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
SWEEP_BUDGETS = [32, 64, 128, 256, 512]


def _scores(make_backend, gen, diff, seeds, budget):
    out = []
    for s in seeds:
        tr = gen(seed=s, difficulty=diff)
        out.append(score_run(run_trace(make_backend(), tr, budget_tokens=budget), tr))
    return out


def _resolve_bge(embed_fn):
    if embed_fn is not None:
        return embed_fn, "injected"
    from aml.backends.vector_only import _default_embedder
    print(f"loading {REFERENCE_MODEL} (first run downloads ~130MB) ...")
    return _default_embedder(), REFERENCE_MODEL


def compare(bge_fn=None, stub_fn=None, seeds=SEEDS, difficulties=DIFFICULTIES, budget=BUDGET):
    bge_fn, label = _resolve_bge(bge_fn)
    stub_fn = stub_fn or _stub_embedder()

    print(f"\nW3 (Distractor Noise): precision under the haystack [{label} vs stub]")
    print(f"budget = {budget} chars   |   seeds = {seeds}\n")
    print(f"  {'diff':7s} {'backend':16s}  {'M1 recall':14s} {'M2 precision':14s}")
    print("  " + "-" * 56)

    builders = [
        ("floor", lambda: PersistenceBackend()),
        ("vector_only(BGE)", lambda: VectorOnlyBackend(embed_fn=bge_fn)),
        ("vector_only(stub)", lambda: VectorOnlyBackend(embed_fn=stub_fn)),
    ]
    for diff in difficulties:
        for name, mk in builders:
            rows = _scores(mk, generate_w3, diff, seeds, budget)
            m1 = mean(r["m1"] for r in rows); s1 = pstdev(r["m1"] for r in rows)
            m2 = mean(r["m2"] for r in rows)
            print(f"  {diff.value:7s} {name:16s}  {m1:5.3f}+/-{s1:5.3f}  {m2:6.3f}")
        print("  " + "-" * 56)
    return bge_fn, stub_fn


def sweep(bge_fn, stub_fn, seeds=SEEDS, budgets=SWEEP_BUDGETS):
    print("\nHard budget sweep — recall (discrimination) + precision, BGE vs stub:\n")
    print(f"  {'budget':7s}  {'BGE rec':8s} {'stub rec':9s}  {'BGE prec':9s} {'stub prec':9s}")
    print("  " + "-" * 50)
    for B in budgets:
        bge = _scores(lambda: VectorOnlyBackend(embed_fn=bge_fn),
                      generate_w3, Difficulty.HARD, seeds, B)
        stb = _scores(lambda: VectorOnlyBackend(embed_fn=stub_fn),
                      generate_w3, Difficulty.HARD, seeds, B)
        print(f"  {B:6d}   {mean(r['m1'] for r in bge):6.3f}  "
              f"{mean(r['m1'] for r in stb):7.3f}   "
              f"{mean(r['m2'] for r in bge):7.3f}  {mean(r['m2'] for r in stb):7.3f}")
    print("  " + "-" * 50)
    print("  (budget-32 recall = recall@1 ~ discrimination: signal vs near-miss)")


def w1_vs_w3(bge_fn, seeds=SEEDS, budget=32):
    print(f"\nNear-miss penalty on a good embedder — BGE recall@{budget}, hard:\n")
    w1 = _scores(lambda: VectorOnlyBackend(embed_fn=bge_fn),
                 generate_w1, Difficulty.HARD, seeds, budget)
    w3 = _scores(lambda: VectorOnlyBackend(embed_fn=bge_fn),
                 generate_w3, Difficulty.HARD, seeds, budget)
    r1, r3 = mean(r["m1"] for r in w1), mean(r["m1"] for r in w3)
    print(f"  W1 (no near-miss):   {r1:.3f}")
    print(f"  W3 (with near-miss): {r3:.3f}")
    print(f"  penalty:             {r3 - r1:+.3f}")


def inertness(bge_fn, seeds=SEEDS, budgets=(32, 512)):
    """On W3 the capability backends are bit-identical to vector_only: no
    superseded_by links (supersede never dispatched) and no as_of (always
    current), so SUPERSESSION_CHAIN and BI_TEMPORAL collapse to a plain vector
    store. Asserted per-seed, contrasted with W2 where the same lever was +0.585."""
    def m1s(mk, gen, diff, B):
        return [score_run(run_trace(mk(), gen(seed=s, difficulty=diff),
                                    budget_tokens=B), gen(seed=s, difficulty=diff))["m1"]
                for s in seeds]

    print("\nCapability inertness on W3 hard (BGE) — capabilities collapse to vector_only:\n")
    print(f"  {'budget':7s}  {'vector':8s} {'supersess':10s} {'bi_temp':9s} {'delta':6s}")
    print("  " + "-" * 46)
    for B in budgets:
        v = m1s(lambda: VectorOnlyBackend(embed_fn=bge_fn), generate_w3, Difficulty.HARD, B)
        s = m1s(lambda: SupersessionVectorBackend(embed_fn=bge_fn), generate_w3, Difficulty.HARD, B)
        t = m1s(lambda: BiTemporalVectorBackend(embed_fn=bge_fn), generate_w3, Difficulty.HARD, B)
        assert v == s == t, f"capabilities NOT inert at budget {B}:\n  v={v}\n  s={s}\n  t={t}"
        print(f"  {B:6d}   {mean(v):6.3f}  {mean(s):8.3f}  {mean(t):7.3f}  {mean(s)-mean(v):+.3f}")
    print("  " + "-" * 46)
    print("  (exact per-seed equality asserted: supersede never dispatched, as_of always None)")

    # Live contrast: the same SUPERSESSION_CHAIN capability on W2, where it was decisive.
    v2 = m1s(lambda: VectorOnlyBackend(embed_fn=bge_fn), generate_w2, Difficulty.HARD, 32)
    s2 = m1s(lambda: SupersessionVectorBackend(embed_fn=bge_fn), generate_w2, Difficulty.HARD, 32)
    print(f"\n  contrast — W2 hard current recall@32: supersession - vector = "
          f"{mean(s2) - mean(v2):+.3f}   (vs +0.000 on W3)")


if __name__ == "__main__":
    bge, stub = compare()
    sweep(bge, stub)
    w1_vs_w3(bge)
    inertness(bge)
    raise SystemExit(0)
