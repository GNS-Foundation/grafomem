"""
GRAFOMEM eval harness — trace runner + per-query scoring (03-eval-metrics.md).

The runner replays a trace against any MemoryBackend in canonical
(timestamp, session_index, turn_index) order:

  - introduce turns  -> backend.write(turn.content); record ref -> fact_ids
  - delete turns      -> backend.delete(ref) iff HARD_DELETE claimed, else no-op
                         (the no-op IS the leak that Check L later catches)
  - query turns       -> flush(); backend.retrieve(turn.content, budget)

Retrieved Memory.ref values are mapped back to fact_ids via the write-time
ledger (refs are opaque join keys, B5), giving per-query retrieved fact sets.

M1–M3 are computed from RunResult by metrics.py. M4 (latency) is recorded here
via per-operation perf_counter instrumentation; M5–M7 are computed in metrics.py
from RunResult + trace metadata.
"""

from __future__ import annotations

import time
from collections import defaultdict
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
    n_na: int = 0                  # queries excluded (capability not claimed, E1)
    # M4 latency: op_type -> list of durations (seconds)
    op_latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))


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
    has_super = Capability.SUPERSESSION_CHAIN in caps
    has_bitemporal = Capability.BI_TEMPORAL in caps
    has_delete = Capability.HARD_DELETE in caps
    has_tenant = Capability.MULTI_TENANT in caps

    fact_by_id = {f.fact_id: f for f in trace.facts}
    # F_old.superseded_by == F_new.fact_id, so this maps new_fid -> old_fid:
    # the predecessor a freshly-introduced fact supersedes.
    predecessor_of = {
        f.superseded_by: f.fact_id
        for f in trace.facts if f.superseded_by is not None
    }

    ref_to_fids: dict[object, set[bytes]] = {}
    fid_to_ref: dict[bytes, object] = {}
    result = RunResult()

    for _ts, _si, _ti, turn in _ordered_turns(trace):
        for fid in turn.introduces:
            fact = fact_by_id.get(fid)
            # valid_from is honored by BI_TEMPORAL backends, silently ignored
            # otherwise (doc 02 §6.2), so it's safe to always pass. Structured
            # subject/predicate metadata is carried for backends that operate on
            # entity keys (e.g. delete-by-subject); ranking backends ignore it,
            # so W1-W4 results are unchanged.
            opts = WriteOptions(
                valid_from=fact.valid_from if fact else None,
                metadata=({"subject": fact.subject, "predicate": fact.predicate}
                          if fact else {}),
                # tenant_id honored by MULTI_TENANT backends, silently None
                # otherwise (vector_only raises on a non-None tenant_id), so
                # W1-W4/W6 are unchanged. The fact's owning tenant is its home.
                tenant_id=(fact.tenant_id if (has_tenant and fact) else None),
            )
            old_fid = predecessor_of.get(fid)
            if has_super and old_fid is not None and old_fid in fid_to_ref:
                t0 = time.perf_counter()
                ref = backend.supersede(fid_to_ref[old_fid], turn.content, opts)
                result.op_latencies['supersede'].append(time.perf_counter() - t0)
            else:
                t0 = time.perf_counter()
                ref = backend.write(turn.content, opts)
                result.op_latencies['write'].append(time.perf_counter() - t0)
            ref_to_fids[ref] = ref_to_fids.get(ref, set()) | {fid}
            fid_to_ref[fid] = ref
            result.n_writes += 1

        if turn.deletes and has_delete:
            for fid in turn.deletes:
                r = fid_to_ref.get(fid)
                if r is not None:
                    t0 = time.perf_counter()
                    backend.delete(r)
                    result.op_latencies['delete'].append(time.perf_counter() - t0)
            # without HARD_DELETE: no-op — content persists (Check L catches it)

        if turn.role == TurnRole.AGENT_QUERY:
            # as_of (historical) queries require BI_TEMPORAL; otherwise the
            # query is N/A for this backend and excluded from Q_W (E1).
            if turn.as_of is not None and not has_bitemporal:
                result.n_na += 1
                continue
            t0 = time.perf_counter()
            backend.flush()
            result.op_latencies['flush'].append(time.perf_counter() - t0)
            opts = RetrieveOptions(
                budget_tokens=budget_tokens,
                as_of=turn.as_of if has_bitemporal else None,
                # the query is issued by its session's tenant; MULTI_TENANT
                # backends scope to it, others see None (no change to W1-W4/W6).
                tenant_id=(trace.sessions[_si].tenant_id if has_tenant else None),
            )
            t0 = time.perf_counter()
            mems = backend.retrieve(turn.content, opts)
            result.op_latencies['retrieve'].append(time.perf_counter() - t0)
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


def run_trace_no_asof(backend, trace: Trace, *, budget_tokens: int) -> RunResult:
    """Paired-run variant for M6: runs the SAME trace but strips as_of from all
    queries, forcing the BI_TEMPORAL backend to answer with current state.

    Historical queries still execute (unlike the normal run where non-BI_TEMPORAL
    backends skip them), but without the as_of timestamp. This lets M6 measure
    the recall advantage of bi-temporal retrieval vs current-only on historical data.
    """
    caps = backend.capabilities()
    has_super = Capability.SUPERSESSION_CHAIN in caps
    has_delete = Capability.HARD_DELETE in caps
    has_tenant = Capability.MULTI_TENANT in caps

    fact_by_id = {f.fact_id: f for f in trace.facts}
    predecessor_of = {
        f.superseded_by: f.fact_id
        for f in trace.facts if f.superseded_by is not None
    }

    ref_to_fids: dict[object, set[bytes]] = {}
    fid_to_ref: dict[bytes, object] = {}
    result = RunResult()

    for _ts, _si, _ti, turn in _ordered_turns(trace):
        for fid in turn.introduces:
            fact = fact_by_id.get(fid)
            opts = WriteOptions(
                valid_from=fact.valid_from if fact else None,
                metadata=({"subject": fact.subject, "predicate": fact.predicate}
                          if fact else {}),
                tenant_id=(fact.tenant_id if (has_tenant and fact) else None),
            )
            old_fid = predecessor_of.get(fid)
            if has_super and old_fid is not None and old_fid in fid_to_ref:
                ref = backend.supersede(fid_to_ref[old_fid], turn.content, opts)
            else:
                ref = backend.write(turn.content, opts)
            ref_to_fids[ref] = ref_to_fids.get(ref, set()) | {fid}
            fid_to_ref[fid] = ref
            result.n_writes += 1

        if turn.deletes and has_delete:
            for fid in turn.deletes:
                r = fid_to_ref.get(fid)
                if r is not None:
                    backend.delete(r)

        if turn.role == TurnRole.AGENT_QUERY:
            backend.flush()
            # KEY DIFFERENCE: as_of is always None — forces current-state retrieval
            opts = RetrieveOptions(
                budget_tokens=budget_tokens,
                as_of=None,
                tenant_id=(trace.sessions[_si].tenant_id if has_tenant else None),
            )
            mems = backend.retrieve(turn.content, opts)
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


# M1/M2/M3 metrics live in aml.eval.metrics. The harness owns only the runner.


# ============================================================================
# Smoke / first real number — run `python -m aml.eval.harness`
# ============================================================================

if __name__ == "__main__":
    from statistics import mean, pstdev

    from aml.backends.persistence import PersistenceBackend
    from aml.generator.trace import Difficulty
    from aml.generator.workloads.w1 import generate_w1
    from aml.eval.metrics import m1_recall

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
            per_seed.append(m1_recall(run, tr))
        overall.extend(per_seed)
        m, sd = mean(per_seed), pstdev(per_seed)
        lo, hi = min(per_seed), max(per_seed)
        print(f"  {diff.value:10s}  {m:6.3f} +/- {sd:5.3f}        "
              f"[{lo:5.3f}, {hi:5.3f}]")

    print("  " + "-" * 48)
    print(f"  {'overall':10s}  {mean(overall):6.3f} +/- {pstdev(overall):5.3f}")

    # Sanity: recency floor must collapse with horizon (easy >> hard).
    easy_m = mean([
        m1_recall(run_trace(PersistenceBackend(),
                              generate_w1(seed=s, difficulty=Difficulty.EASY),
                              budget_tokens=BUDGET),
                    generate_w1(seed=s, difficulty=Difficulty.EASY))
        for s in SEEDS
    ])
    hard_m = mean([
        m1_recall(run_trace(PersistenceBackend(),
                              generate_w1(seed=s, difficulty=Difficulty.HARD),
                              budget_tokens=BUDGET),
                    generate_w1(seed=s, difficulty=Difficulty.HARD))
        for s in SEEDS
    ])
    assert easy_m > hard_m, f"floor should decay with horizon; easy={easy_m} hard={hard_m}"
    print(f"\n✓ Recency floor decays with horizon  (easy {easy_m:.3f} > hard {hard_m:.3f})")
    print("\nFirst real M1 numbers on the W1 corpus. The floor is established.")
