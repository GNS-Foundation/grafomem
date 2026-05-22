#!/usr/bin/env python3
"""
Scale probe: write + retrieve latency of the SQLite + sqlite-vec GMP store on the
locked grafomem-bench-v0.1.8 corpus, head-to-head against brute-force VectorOnly.

Unlike the experiment scripts (fresh backend per trace), this drives the corpus
through `run_trace` onto a SINGLE store that GROWS across traces — so retrieve
latency is measured against an increasing store size N. Recall is meaningless here
(cross-trace mixing); we want timings and a growing N, nothing else.

  Phase A — full locked corpus (W1..W6) -> SQLite store. Per-op latency + a
            retrieve-latency-vs-N curve. This is the store's real profile.
  Phase B — pure-vector workloads only (W1+W3: no supersession / delete / tenant /
            as_of, so the op sequence is identical for both backends) -> SQLite
            (sqlite-vec ANN) vs VectorOnly (brute-force numpy). Retrieve latency by
            matched N bucket. Finds the crossover, if any — at small N brute force
            can win, since the ANN index only pays off past some size.

One shared BGE instance is handed to every backend (no per-store model reload).

Usage:  python -m scripts.scale_probe
"""

from __future__ import annotations

import time

import numpy as np

from aml.backends.sqlite_gmp import SQLiteGMPBackend
from aml.backends.vector_only import VectorOnlyBackend, _default_embedder
from aml.eval.harness import run_trace
from aml.generator.trace import Difficulty
from aml.generator.workloads.w1 import generate_w1
from aml.generator.workloads.w2 import generate_w2
from aml.generator.workloads.w3 import generate_w3
from aml.generator.workloads.w4 import generate_w4
from aml.generator.workloads.w5 import generate_w5
from aml.generator.workloads.w6 import generate_w6

BUDGET = 512
SEEDS = [0, 1, 2, 3, 4]
DIFFS = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
GEN = {1: generate_w1, 2: generate_w2, 3: generate_w3,
       4: generate_w4, 5: generate_w5, 6: generate_w6}
BINS = [0, 1000, 2500, 5000, 10000, 25000, 50000, 10**9]


class TimingBackend:
    """Wraps a MemoryBackend, timing each op and tracking index size N."""

    def __init__(self, inner):
        self.inner = inner
        self.writes: list[float] = []
        self.supersedes: list[float] = []
        self.deletes: list[float] = []
        self.retrieves: list[tuple[int, float]] = []   # (N at call time, seconds)
        self._rows = 0

    def capabilities(self):
        return self.inner.capabilities()

    def write(self, content, options):
        t = time.perf_counter()
        ref = self.inner.write(content, options)
        self.writes.append(time.perf_counter() - t)
        self._rows += 1
        return ref

    def supersede(self, old_ref, content, options):
        t = time.perf_counter()
        ref = self.inner.supersede(old_ref, content, options)
        self.supersedes.append(time.perf_counter() - t)
        self._rows += 1                                  # supersede adds a (closed) version row
        return ref

    def delete(self, ref):
        t = time.perf_counter()
        ok = self.inner.delete(ref)
        self.deletes.append(time.perf_counter() - t)
        if ok:
            self._rows -= 1
        return ok

    def retrieve(self, query, options):
        t = time.perf_counter()
        out = self.inner.retrieve(query, options)
        self.retrieves.append((self._rows, time.perf_counter() - t))
        return out

    def audit(self):
        return self.inner.audit()

    def flush(self):
        return self.inner.flush()


def corpus(workload_ids, seeds=SEEDS, diffs=DIFFS):
    return [GEN[w](seed=s, difficulty=d)
            for w in workload_ids for s in seeds for d in diffs]


def drive(backend: TimingBackend, traces, label: str = "") -> TimingBackend:
    total = len(traces)
    for i, tr in enumerate(traces, 1):
        run_trace(backend, tr, budget_tokens=BUDGET)
        print(f"\r  {label}{i}/{total} traces ingested, N={backend._rows:,}   ",
              end="", flush=True)
    print()
    return backend


def _ms(xs, p):
    return float(np.percentile(xs, p)) * 1000.0 if xs else float("nan")


def _bucket_label(lo, hi):
    if hi >= 10**9:
        return f"{lo // 1000}k+"
    if lo == 0:
        return f"<{hi // 1000}k"
    return f"{lo // 1000}k-{hi // 1000}k"


def report_profile(tb: TimingBackend) -> None:
    print(f"  rows in index (N): {tb._rows:,}")
    if tb.writes:
        print(f"  writes:     {len(tb.writes):>6,}   p50 {_ms(tb.writes, 50):5.2f}ms  "
              f"p95 {_ms(tb.writes, 95):5.2f}ms   ({len(tb.writes) / sum(tb.writes):,.0f}/s)")
    if tb.supersedes:
        print(f"  supersedes: {len(tb.supersedes):>6,}   p50 {_ms(tb.supersedes, 50):5.2f}ms  "
              f"p95 {_ms(tb.supersedes, 95):5.2f}ms")
    if tb.deletes:
        print(f"  deletes:    {len(tb.deletes):>6,}   p50 {_ms(tb.deletes, 50):5.2f}ms  "
              f"p95 {_ms(tb.deletes, 95):5.2f}ms")
    rt = [dt for _, dt in tb.retrieves]
    if rt:
        print(f"  retrieves:  {len(rt):>6,}   p50 {_ms(rt, 50):5.2f}ms  p95 {_ms(rt, 95):5.2f}ms")
    print("\n  retrieve latency vs store size:")
    for lo, hi in zip(BINS, BINS[1:]):
        sub = [dt for n, dt in tb.retrieves if lo <= n < hi]
        if sub:
            print(f"    {_bucket_label(lo, hi):>8s}   p50 {_ms(sub, 50):5.2f}ms  "
                  f"p95 {_ms(sub, 95):5.2f}ms   (n={len(sub):,})")


def report_compare(sq: TimingBackend, bf: TimingBackend) -> None:
    print(f"  {'N bucket':>8s}   {'sqlite p50/p95 (ms)':>20s}   {'brute p50/p95 (ms)':>20s}")
    print("  " + "-" * 56)
    crossover = None
    for lo, hi in zip(BINS, BINS[1:]):
        s = [dt for n, dt in sq.retrieves if lo <= n < hi]
        b = [dt for n, dt in bf.retrieves if lo <= n < hi]
        if not (s or b):
            continue
        sp, bp = _ms(s, 50), _ms(b, 50)
        print(f"  {_bucket_label(lo, hi):>8s}   {sp:8.2f} / {_ms(s, 95):<8.2f}   "
              f"{bp:8.2f} / {_ms(b, 95):<8.2f}")
        if crossover is None and s and b and sp < bp:
            crossover = _bucket_label(lo, hi)
    print("  " + "-" * 56)
    if crossover:
        print(f"  crossover: sqlite-vec overtakes brute force at N ~ {crossover}")
    else:
        print("  no crossover in range — brute force stays ahead at this scale "
              "(ANN overhead not yet amortized)")


if __name__ == "__main__":
    shared = _default_embedder()                          # one BGE, reused everywhere

    print("GRAFOMEM scale probe — locked corpus grafomem-bench-v0.1.8\n")
    print("Phase A — full corpus (W1..W6) -> SQLite + sqlite-vec store")
    print("  (ingesting; BGE-embeds every write and query, so this takes a minute or two)\n")
    full = corpus([1, 2, 3, 4, 5, 6])
    sq_full = drive(TimingBackend(SQLiteGMPBackend(":memory:", embed_fn=shared)), full, "A: ")
    report_profile(sq_full)

    print("\nPhase B — pure-vector workloads (W1+W3): sqlite-vec ANN vs brute force\n")
    pv = corpus([1, 3])
    sq_pv = drive(TimingBackend(SQLiteGMPBackend(":memory:", embed_fn=shared)), pv, "B sqlite: ")
    bf_pv = drive(TimingBackend(VectorOnlyBackend(embed_fn=shared)), pv, "B brute:  ")
    report_compare(sq_pv, bf_pv)

    print("\ndone.")
    raise SystemExit(0)
