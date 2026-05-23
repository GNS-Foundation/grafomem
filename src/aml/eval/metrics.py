"""
GRAFOMEM evaluation metrics — M1–M7 + paired bootstrap CI (doc 03 §4).

All three are computed from a RunResult (per-query retrieved fact sets + return
counts + returned content size) against a trace's GroundTruth.recall_targets.

  M1 — Recall@K   mean over queries of |retrieved ∩ targets| / |targets|
  M2 — Precision@K mean over (non-empty) queries of |retrieved ∩ targets| / returned
  M3 — tokens-per-correct-fact, POOLED: Σ returned_size / Σ |TP|  (lower better)

M3 currently uses the char-proxy that the runner records (same unit as the
retrieve budget, so it's internally consistent and valid for relative
comparison). doc 03 §3 pins cl100k_base for absolute cross-backend reporting;
that is a one-line swap once a tokenizer is wired into the runner — deferred to
keep the dependency surface minimal. Both backends are measured in the same
unit, so the vector-vs-floor M3 ratio is already meaningful.
"""

from __future__ import annotations

import random
from statistics import mean

from aml.eval.harness import RunResult
from aml.generator.trace import Trace


def _targets_by_turn(trace: Trace) -> dict[str, set[bytes]]:
    return {str(tid): t for tid, t in trace.ground_truth.recall_targets.items()}


def m1_recall(run: RunResult, trace: Trace) -> float:
    """M1 — mean per-query recall. Queries with empty targets skipped (E1)."""
    tgt = _targets_by_turn(trace)
    vals = []
    for qr in run.per_query:
        targets = tgt.get(qr.turn_id, set())
        if not targets:
            continue
        vals.append(len(qr.retrieved & targets) / len(targets))
    return mean(vals) if vals else 0.0


def m2_precision(run: RunResult, trace: Trace) -> float:
    """M2 — mean per-query precision over queries that returned something.
    A query that returns nothing has no precision to measure and is skipped."""
    tgt = _targets_by_turn(trace)
    vals = []
    for qr in run.per_query:
        if qr.n_returned == 0:
            continue
        targets = tgt.get(qr.turn_id, set())
        vals.append(len(qr.retrieved & targets) / qr.n_returned)
    return mean(vals) if vals else 0.0


def m3_tokens_per_fact(run: RunResult, trace: Trace) -> float:
    """M3 — pooled returned-size per correct fact. inf if no correct facts."""
    tgt = _targets_by_turn(trace)
    total_size = 0
    total_tp = 0
    for qr in run.per_query:
        targets = tgt.get(qr.turn_id, set())
        total_size += qr.content_chars
        total_tp += len(qr.retrieved & targets)
    return (total_size / total_tp) if total_tp > 0 else float("inf")


def m4_latency(run: RunResult) -> dict[str, dict[str, float]]:
    """M4 — per-operation latency percentiles (P50/P95/P99), in milliseconds.
    Returns a dict keyed by operation type (write, supersede, delete, retrieve,
    flush). Empty operations are omitted."""
    import math
    result = {}
    for op, durations in run.op_latencies.items():
        if not durations:
            continue
        s = sorted(durations)
        n = len(s)
        result[op] = {
            "p50": s[n // 2] * 1000,
            "p95": s[min(math.ceil(n * 0.95) - 1, n - 1)] * 1000,
            "p99": s[min(math.ceil(n * 0.99) - 1, n - 1)] * 1000,
            "count": n,
        }
    return result


def m5_storage(backend) -> dict[str, float] | None:
    """M5 — storage footprint. Returns bytes_per_fact and total_bytes if the
    backend implements storage_bytes(); otherwise None."""
    fn = getattr(backend, "storage_bytes", None)
    if fn is None:
        return None
    total = fn()
    if total is None:
        return None
    # n_facts is not available here; caller supplies it.
    return {"total_bytes": total}


def m7_tenant_isolation(run: RunResult, trace: Trace) -> float | None:
    """M7 — tenant isolation score for W5 trap queries.
    Returns 1 - (leaked queries / trap queries), or None if no trap queries.
    A trap query is one whose requires set is empty (the answer doesn’t exist
    in this tenant). A leak is when retrieved is non-empty for such a query."""
    tgt = _targets_by_turn(trace)
    traps = 0
    leaks = 0
    for qr in run.per_query:
        targets = tgt.get(qr.turn_id, set())
        if not targets:  # trap query: no valid facts for this tenant
            traps += 1
            if qr.retrieved:  # backend returned something -> leak
                leaks += 1
    if traps == 0:
        return None
    return 1.0 - (leaks / traps)


def score_run(run: RunResult, trace: Trace) -> dict:
    """All available metrics for one run. M1–M3 always; M4 if latency data exists;
    M5/M6/M7 computed externally (not from a single RunResult)."""
    scores: dict = {
        "m1": m1_recall(run, trace),
        "m2": m2_precision(run, trace),
        "m3": m3_tokens_per_fact(run, trace),
    }
    lat = m4_latency(run)
    if lat:
        scores["m4"] = lat
    return scores


def bootstrap_paired_ci(
    baseline: list[float],
    candidate: list[float],
    *,
    n_resamples: int = 10_000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """95% CI for mean(candidate) - mean(baseline) via paired bootstrap over
    the per-unit (e.g. per-seed) values. Returns (point_estimate, lo, hi).

    A claim "candidate beats baseline" holds iff lo > 0 (doc 03 §9)."""
    if len(baseline) != len(candidate):
        raise ValueError("paired bootstrap needs equal-length samples")
    diffs = [c - b for b, c in zip(baseline, candidate)]
    k = len(diffs)
    rng = random.Random(seed)
    boot = []
    for _ in range(n_resamples):
        boot.append(sum(diffs[rng.randrange(k)] for _ in range(k)) / k)
    boot.sort()
    lo = boot[int((alpha / 2) * n_resamples)]
    hi = boot[int((1 - alpha / 2) * n_resamples)]
    return (mean(diffs), lo, hi)


# ============================================================================
# Smoke check — run `python -m aml.eval.metrics`
# ============================================================================

if __name__ == "__main__":
    from aml.backends.persistence import PersistenceBackend
    from aml.backends.vector_only import VectorOnlyBackend, _stub_embedder
    from aml.generator.trace import Difficulty
    from aml.generator.workloads.w1 import generate_w1
    from aml.eval.harness import run_trace

    print("GRAFOMEM metrics.py — M1 / M2 / M3 + bootstrap CI\n")

    tr = generate_w1(seed=0, difficulty=Difficulty.HARD)

    floor = score_run(run_trace(PersistenceBackend(), tr, budget_tokens=512), tr)
    vec = score_run(
        run_trace(VectorOnlyBackend(embed_fn=_stub_embedder()), tr,
                  budget_tokens=512), tr)

    # M1 in [0,1], floor < vector on hard.
    assert 0.0 <= floor["m1"] <= 1.0 and 0.0 <= vec["m1"] <= 1.0
    assert vec["m1"] > floor["m1"], (vec["m1"], floor["m1"])
    print(f"✓ M1 recall                          (floor {floor['m1']:.3f} < "
          f"vector {vec['m1']:.3f})")

    # Precision is low when the budget floods (returns ~20 for a 1-fact query).
    assert vec["m2"] < 0.3, vec["m2"]
    print(f"✓ M2 precision exposes the flood     (vector M2 = {vec['m2']:.3f} "
          f"<- recall 1.0 hides this)")

    # M3 pooled, lower is better; floods cost more tokens per correct fact.
    assert vec["m3"] > 0 and floor["m3"] > 0
    print(f"✓ M3 tokens/correct-fact (pooled)    (vector {vec['m3']:.1f} vs "
          f"floor {floor['m3']:.1f} char/fact)")

    # Bootstrap CI excludes zero for a real difference.
    fl = [m1_recall(run_trace(PersistenceBackend(),
                              generate_w1(seed=s, difficulty=Difficulty.HARD),
                              budget_tokens=512),
                    generate_w1(seed=s, difficulty=Difficulty.HARD))
          for s in range(5)]
    ve = [m1_recall(run_trace(VectorOnlyBackend(embed_fn=_stub_embedder()),
                              generate_w1(seed=s, difficulty=Difficulty.HARD),
                              budget_tokens=512),
                    generate_w1(seed=s, difficulty=Difficulty.HARD))
          for s in range(5)]
    point, lo, hi = bootstrap_paired_ci(fl, ve)
    assert lo > 0, (point, lo, hi)
    print(f"✓ Paired bootstrap CI excludes zero  (ΔM1 = {point:+.3f}, "
          f"95% CI [{lo:+.3f}, {hi:+.3f}])")

    print("\nAll metrics smoke checks green. Ready to re-run W1 with the full "
          "M1/M2/M3 picture.")
