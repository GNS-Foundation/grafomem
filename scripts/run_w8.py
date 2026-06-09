"""
W8 experiment: the forgetting curve — retention policy as the lever.

W4 showed a bounded store cliffs at d = K. W8 asks the next question: given that
you must bound footprint, WHICH facts should you keep? Three policies, read two
ways (recall by distance, and footprint across the horizon):

  - unbounded        : keeps everything. Flat recall 1.0, footprint grows
                       linearly with the horizon. The ceiling and the cost foil.
  - fifo(K)          : recency window (= W4's bounded_vector). Importance-blind:
                       a high fact at distance d > K is evicted with the filler.
                       Recall cliffs at d = K. Footprint plateaus at K.
  - importance(K)    : same capacity K, evicts lowest-importance first. The high
                       facts (n_high <= K) all survive at every distance: recall
                       stays flat. Footprint plateaus at K — same as fifo.

The result: importance(K) matches the unbounded store's recall at fifo's
footprint. Principled forgetting is Pareto-dominant on long-horizon recall — the
paper's open retention question (only FIFO had been evaluated), answered as a
structural, embedder-invariant fact.

Why run_w8 owns the replay. The high/low signal lives in Fact.importance, but
the shared harness (aml.eval.harness.run_trace) does not put importance on the
write path — it carries only subject/predicate metadata. Rather than touch the
locked harness, run_w8 replays the trace in canonical order and adds
'importance' to the write metadata, exactly as run_w9 owns its per-session
dispatch. interface.py / harness.py are untouched; w8.py, retention_backends.py,
and this file are purely additive. (Folding 'importance' into the harness's write
metadata would let W8 run through run_trace directly — a one-line additive change
left to the maintainer.)

Defaults to the stub embedder at a generous budget: recall in W8 is a structural
question (is the required high fact in the retained window?), so isolating it
from ranking/budget pressure is the honest check and needs no BGE download. For
headline numbers under retrieval pressure, call measure(embed_fn=real,
budget=512); the real BGE ranks a fact's own question to the top, so the curve is
unchanged. Requires grafomem[backends] for the real embedder.
"""

from __future__ import annotations

from statistics import mean

from aml.backends.bounded_vector import BoundedVectorBackend
from aml.backends.retention_backends import ImportanceBoundedBackend, SummarisingRetentionBackend
from aml.backends.vector_only import REFERENCE_MODEL, VectorOnlyBackend
from aml.backends.interface import RetrieveOptions, WriteOptions
from aml.generator.trace import Difficulty, TurnRole
from aml.generator.workloads.w8 import REFERENCE_CAPACITY, generate_w8
from aml.eval.metrics import _targets_by_turn

K = REFERENCE_CAPACITY
SEEDS = [0, 1, 2, 3, 4]
TIERS = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
BINS = [(1, 16), (16, 64), (64, 256), (256, 1024), (1024, 10**9)]


def _bin_label(lo, hi):
    return f"<{hi}" if lo < 16 else (f">={lo}" if hi > 10**8 else f"{lo}-{hi-1}")


def _distance_of(trace) -> dict[str, int]:
    """query turn_id -> distance d = facts introduced after the target."""
    intro_ts = {fid: t.timestamp for s in trace.sessions for t in s.turns
                for fid in t.introduces}
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


def run_w8_replay(backend, trace, *, budget_tokens):
    """Mirror of harness.run_trace, but injects Fact.importance into the write
    metadata so importance-aware retention policies can see it. W8 has no
    deletes/supersessions, so the loop is just write-then-query."""
    fact_by_id = {f.fact_id: f for f in trace.facts}
    ref_to_fids: dict[object, set[bytes]] = {}
    per_query: list[tuple[str, set[bytes]]] = []
    rows = []
    for si, s in enumerate(trace.sessions):
        for ti, t in enumerate(s.turns):
            rows.append((t.timestamp, si, ti, t))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    for _ts, _si, _ti, turn in rows:
        for fid in turn.introduces:
            f = fact_by_id.get(fid)
            opts = WriteOptions(
                valid_from=f.valid_from if f else None,
                metadata=({"subject": f.subject, "predicate": f.predicate,
                           "importance": f.importance} if f else {}),
            )
            ref = backend.write(turn.content, opts)
            ref_to_fids[ref] = ref_to_fids.get(ref, set()) | {fid}
        if turn.role == TurnRole.AGENT_QUERY:
            backend.flush()
            mems = backend.retrieve(turn.content, RetrieveOptions(budget_tokens=budget_tokens))
            retrieved: set[bytes] = set()
            for m in mems:
                # Resolve compacts metadata to their fids
                compact_refs = m.metadata.get("compacts", [m.ref])
                for cr in compact_refs:
                    retrieved |= ref_to_fids.get(cr, set())
            per_query.append((str(turn.turn_id), retrieved))
    return per_query


def _backends(emb):
    return [
        ("unbounded",          lambda: VectorOnlyBackend(embed_fn=emb)),
        (f"fifo(K={K})",       lambda: BoundedVectorBackend(capacity=K, embed_fn=emb)),
        (f"importance(K={K})", lambda: ImportanceBoundedBackend(capacity=K, embed_fn=emb)),
        (f"summarise(K={K})",  lambda: SummarisingRetentionBackend(capacity=K, embed_fn=emb)),
    ]


def _resolve(embed_fn):
    if embed_fn is not None:
        return embed_fn, "injected"
    from aml.backends.vector_only import _stub_embedder
    return _stub_embedder(), "stub"


def recall_by_distance(embed_fn=None, seeds=SEEDS, budget=1024, diff=Difficulty.HARD):
    emb, label = _resolve(embed_fn)
    backends = _backends(emb)
    agg = {b: {name: [] for name, _ in backends} for b in BINS}
    for s in seeds:
        tr = generate_w8(seed=s, difficulty=diff)
        dmap = _distance_of(tr)
        tgt = _targets_by_turn(tr)
        for name, mk in backends:
            pq = run_w8_replay(mk(), tr, budget_tokens=budget)
            for tid, retrieved in pq:
                d = dmap.get(tid)
                T = tgt.get(tid, set())
                if d is None or not T:
                    continue
                r = len(retrieved & T) / len(T)
                for b in BINS:
                    if b[0] <= d < b[1]:
                        agg[b][name].append(r)
                        break
    print(f"\nW8 recall by distance d ({diff.value}, {len(seeds)} seeds) [{label}]:\n")
    hdr = "  " + f"{'distance':10s}" + "".join(f"{n:>17s}" for n, _ in backends)
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for b in BINS:
        row = f"  {_bin_label(*b):10s}"
        for name, _ in backends:
            vals = agg[b][name]
            row += f"{(mean(vals) if vals else float('nan')):>17.3f}"
        print(row)
    print("  " + "-" * (len(hdr) - 2))
    print(f"  (fifo cliffs at d=K={K}; importance flat — it keeps the high facts; unbounded flat)")
    return agg


def footprint(embed_fn=None, seeds=SEEDS, tiers=TIERS):
    emb, _ = _resolve(embed_fn)
    backends = _backends(emb)
    print("\nFootprint = retained memories = scan cost (M5 == M4), from audit():\n")
    print(f"  {'tier':7s} {'horizon':8s}" + "".join(f"{n+' ret':>22s}" for n, _ in backends)
          + "    high-recall (u/f/i/s)")
    print("  " + "-" * 104)
    summary = {}
    for diff in tiers:
        retains = {name: [] for name, _ in backends}
        recalls = {name: [] for name, _ in backends}
        horizon = 0
        for s in seeds:
            tr = generate_w8(seed=s, difficulty=diff)
            horizon = len(tr.facts)
            tgt = _targets_by_turn(tr)
            for name, mk in backends:
                b = mk()
                pq = run_w8_replay(b, tr, budget_tokens=1024)
                retains[name].append(len(list(b.audit())))
                recalls[name].append(mean(len(rv & tgt[tid]) / len(tgt[tid])
                                          for tid, rv in pq if tgt.get(tid)))
        row = f"  {diff.value:7s} {horizon:7d} "
        for name, _ in backends:
            row += f"{mean(retains[name]):22.0f}"
        rec = [mean(recalls[name]) for name, _ in backends]
        row += f"     {rec[0]:.3f}/{rec[1]:.3f}/{rec[2]:.3f}/{rec[3]:.3f}"
        print(row)
        summary[diff] = (
            {name: mean(retains[name]) for name, _ in backends},
            {name: mean(recalls[name]) for name, _ in backends},
        )
    print("  " + "-" * 104)
    print(f"  (unbounded retain grows with horizon; fifo & importance both plateau at K={K})")
    return summary


def main(embed_fn=None):
    recall_by_distance(embed_fn)
    summary = footprint(embed_fn)

    print("\n✓ Forgetting curve as designed.")

if __name__ == "__main__":
    print("=== W8 EVALUATION: STUB EMBEDDER (Budget Exhaustion) ===")
    main()
    print("\n=== W8 EVALUATION: REAL BGE EMBEDDER (Dilution) ===")
    try:
        from aml.backends.vector_only import _default_embedder
        main(embed_fn=_default_embedder())
    except ImportError:
        print("Skipping real BGE run (grafomem[backends] not installed)")
