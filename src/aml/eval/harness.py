"""
GRAFOMEM eval harness — trace runner + M1 Recall@K (03-eval-metrics.md §4.1).

The runner replays a trace against any MemoryBackend in canonical
(timestamp, session_index, turn_index) order:

  - introduce turns  -> backend.write(turn.content); record ref -> fact_ids
  - delete turns      -> backend.delete(ref) iff HARD_DELETE claimed, else no-op
                         (the no-op IS the leak that Check L later catches)
  - query turns       -> flush(); backend.retrieve(turn.content, budget)

Retrieved Memory.ref values are mapped back to fact_ids via the write-time
ledger (refs are opaque join keys, B5), giving per-query retrieved fact sets.

M1 is then the mean per-query recall against GroundTruth.recall_targets.

Supersession dispatch is intentionally not handled yet — W2 will define how the
turn stream encodes supersession, and the runner gains a branch then. W1 has
neither deletes nor supersessions, so this runner scores it end to end today.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aml.backends.interface import (
    Capability,
    RetrieveOptions,
    WriteOptions,
)
from aml.generator.trace import Trace, TurnRole


@dataclass(slots=True)
class QueryRun:
    turn_id: str
    retrieved: set[bytes]          # fact_ids the backend surfaced
    n_returned: int                # how many Memory objects came back
    content_chars: int             # total chars returned (token proxy)


@dataclass(slots=True)
class RunResult:
    per_query: list[QueryRun] = field(default_factory=list)
    n_writes: int = 0


def _ordered_turns(trace: Trace):
    """Yield (timestamp, session_index, turn_index, turn) in canonical order."""
    rows = []
    for si, session in enumerate(trace.sessions):
        for ti, turn in enumerate(session.turns):
            rows.append((turn.timestamp, si, ti, turn))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return rows


def run_trace(backend, trace: Trace, *, budget_tokens: int) -> RunResult:
    caps = backend.capabilities()
    ref_to_fids: dict[object, set[bytes]] = {}
    fid_to_ref: dict[bytes, object] = {}
    result = RunResult()

    for _ts, _si, _ti, turn in _ordered_turns(trace):
        if turn.introduces:
            ref = backend.write(turn.content, WriteOptions())
            ref_to_fids[ref] = set(turn.introduces)
            for fid in turn.introduces:
                fid_to_ref[fid] = ref
            result.n_writes += 1

        if turn.deletes:
            if Capability.HARD_DELETE in caps:
                for fid in turn.deletes:
                    r = fid_to_ref.get(fid)
                    if r is not None:
                        backend.delete(r)
            # else: no-op — content persists (the leak Check L catches)

        if turn.role == TurnRole.AGENT_QUERY:
            backend.flush()
            mems = backend.retrieve(
                turn.content, RetrieveOptions(budget_tokens=budget_tokens),
            )
            retrieved: set[bytes] = set()
            for m in mems:
                retrieved |= ref_to_fids.get(m.ref, set())
            result.per_query.append(QueryRun(
                turn_id=str(turn.turn_id),
                retrieved=retrieved,
                n_returned=len(mems),
                content_chars=sum(len(m.content) for m in mems),
            ))

    return result


def recall_at_k(run: RunResult, trace: Trace) -> float:
    """M1 — mean per-query recall against recall_targets. Queries with empty
    targets are skipped (E1)."""
    gt = trace.ground_truth
    recalls: list[float] = []
    for qr in run.per_query:
        # recall_targets keyed by turn_id (UUID); QueryRun stores str(turn_id).
        targets = next(
            (t for tid, t in gt.recall_targets.items() if str(tid) == qr.turn_id),
            set(),
        )
        if not targets:
            continue
        recalls.append(len(qr.retrieved & targets) / len(targets))
    return sum(recalls) / len(recalls) if recalls else 0.0


# ============================================================================
# Smoke / first real number — run `python -m aml.eval.harness`
# ============================================================================

if __name__ == "__main__":
    from statistics import mean, pstdev

    from aml.backends.persistence import PersistenceBackend
    from aml.generator.trace import Difficulty
    from aml.generator.workloads.w1 import generate_w1

    print("GRAFOMEM eval harness — persistence floor M1 on W1\n")

    # Character budget (token proxy). ~30-45 chars per W1 fact, so 512 chars
    # is a window of roughly a dozen recent facts.
    BUDGET = 512
    SEEDS = range(5)

    print(f"budget_tokens = {BUDGET} chars   |   seeds = {list(SEEDS)}\n")
    print(f"  {'difficulty':10s}  {'M1 (mean +/- sd)':20s}  {'range':14s}")
    print("  " + "-" * 48)

    overall: list[float] = []
    for diff in (Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD):
        per_seed = []
        for seed in SEEDS:
            tr = generate_w1(seed=seed, difficulty=diff)
            run = run_trace(PersistenceBackend(), tr, budget_tokens=BUDGET)
            per_seed.append(recall_at_k(run, tr))
        overall.extend(per_seed)
        m, sd = mean(per_seed), pstdev(per_seed)
        lo, hi = min(per_seed), max(per_seed)
        print(f"  {diff.value:10s}  {m:6.3f} +/- {sd:5.3f}        "
              f"[{lo:5.3f}, {hi:5.3f}]")

    print("  " + "-" * 48)
    print(f"  {'overall':10s}  {mean(overall):6.3f} +/- {pstdev(overall):5.3f}")

    # Sanity: recency floor must collapse with horizon (easy >> hard).
    easy_m = mean([
        recall_at_k(run_trace(PersistenceBackend(),
                              generate_w1(seed=s, difficulty=Difficulty.EASY),
                              budget_tokens=BUDGET),
                    generate_w1(seed=s, difficulty=Difficulty.EASY))
        for s in SEEDS
    ])
    hard_m = mean([
        recall_at_k(run_trace(PersistenceBackend(),
                              generate_w1(seed=s, difficulty=Difficulty.HARD),
                              budget_tokens=BUDGET),
                    generate_w1(seed=s, difficulty=Difficulty.HARD))
        for s in SEEDS
    ])
    assert easy_m > hard_m, f"floor should decay with horizon; easy={easy_m} hard={hard_m}"
    print(f"\n✓ Recency floor decays with horizon  (easy {easy_m:.3f} > hard {hard_m:.3f})")
    print("\nFirst real M1 numbers on the W1 corpus. The floor is established.")
