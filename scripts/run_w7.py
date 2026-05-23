"""
W7 experiment: conflict detection — how a backend resolves two simultaneously-
valid writes to the same (subject, predicate).

Unlike W1-W6, W7 does not yield a scalar score; it yields a *behavior class*
per backend (01-workload-spec.md §4.7). Each W7 unit writes two facts to one
(subject, predicate) slot — distinct objects, both left live (neither
superseded) — then asks one conflict query whose `requires` is the contested
pair [earlier, later] (by sequence). What the backend surfaces for that slot is
the whole measurement:

    {later}            -> last_write_wins
    {earlier}          -> first_write_wins
    {earlier, later}   -> merge
    neither            -> silent_data_loss
    explicit signal    -> conflict_flag        (requires CONFLICT_DETECTION)
    flips across runs  -> non_deterministic     (cross-replay verdict)

The conflict signal rides on the existing free-form `Memory.metadata`: a
CONFLICT_DETECTION backend marks a contested Memory `metadata["conflict"]=True`.
Nothing in the interface or harness changes — `run_w7` captures the marker via
a transparent recording proxy (`_Recorder`) wrapped around the backend, so
`run_trace` runs exactly as it does for every other workload. Because W7 queries
never use `as_of`, the proxy's retrieve calls align 1:1, in order, with
`run.per_query`.

Six toy backends (aml.backends.conflict_backends), one per class, make the
capability map concrete and let this runner self-check: each must land in the
class it was built to embody.

Embedding: the STUB embedder by default. W7 measures conflict RESOLUTION (which
writes a backend keeps / surfaces), which is independent of embedding quality —
the stub's lexical match is sufficient to surface a slot's facts for its query,
and it needs no model download. (Contrast W1/W6, which test retrieval quality
and use real BGE.) Pass embed_fn=... to override.

Run:  python scripts/run_w7.py
"""

from __future__ import annotations

from aml.backends.conflict_backends import (
    ConflictAwareBackend,
    FirstWriteWinsBackend,
    FlakyBackend,
    LastWriteWinsBackend,
    MergeBackend,
    SilentDataLossBackend,
)
from aml.backends.vector_only import _stub_embedder
from aml.eval.harness import run_trace
from aml.generator.trace import Difficulty, TurnRole
from aml.generator.workloads.w7 import generate_w7

from w7_classify import BehaviorClass, classify_backend, classify_query

SEEDS = range(5)
BUDGET = 1 << 20          # effectively unlimited; W7 tests resolution, not budget
DIFF = Difficulty.HARD
N_REPLAYS = 2             # two replays per (backend, seed) expose stochastic backends


class _Recorder:
    """Transparent MemoryBackend proxy. Records, per retrieve call (in order),
    whether any returned Memory carried metadata['conflict']. Everything else
    delegates unchanged, so run_trace is unaffected."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.conflict_flags: list[bool] = []      # one per retrieve call, in order

    def capabilities(self):
        return self._inner.capabilities()

    def write(self, content, options):
        return self._inner.write(content, options)

    def supersede(self, old_ref, content, options):
        return self._inner.supersede(old_ref, content, options)

    def delete(self, ref):
        return self._inner.delete(ref)

    def retrieve(self, query, options):
        mems = self._inner.retrieve(query, options)
        self.conflict_flags.append(any(m.metadata.get("conflict") for m in mems))
        return mems

    def audit(self):
        return self._inner.audit()

    def flush(self):
        self._inner.flush()


def _contested(trace) -> dict[str, tuple[bytes, bytes]]:
    """turn_id -> (earlier_id, later_id) ordered by sequence, for each conflict
    query (the AGENT_QUERY turns with a two-fact `requires`)."""
    seq = {f.fact_id: f.sequence for f in trace.facts}
    out: dict[str, tuple[bytes, bytes]] = {}
    for s in trace.sessions:
        for t in s.turns:
            if t.role == TurnRole.AGENT_QUERY and len(t.requires) == 2:
                lo, hi = sorted(t.requires, key=lambda fid: seq[fid])
                out[str(t.turn_id)] = (lo, hi)
    return out


def measure(embed_fn=None, seeds=SEEDS, n_replays=N_REPLAYS):
    emb = embed_fn or _stub_embedder()
    # mk takes the replay index: deterministic backends ignore it; the flaky
    # one seeds its RNG from it, so its two replays disagree (modeling a
    # genuinely stochastic backend across runs).
    backends = [
        ("merge",            lambda r: MergeBackend(embed_fn=emb)),
        ("last_write_wins",  lambda r: LastWriteWinsBackend(embed_fn=emb)),
        ("first_write_wins", lambda r: FirstWriteWinsBackend(embed_fn=emb)),
        ("silent_data_loss", lambda r: SilentDataLossBackend(embed_fn=emb)),
        ("conflict_aware",   lambda r: ConflictAwareBackend(embed_fn=emb)),
        ("flaky",            lambda r: FlakyBackend(embed_fn=emb, seed=r)),
    ]
    results: dict[str, list[dict]] = {name: [] for name, _ in backends}
    for s in seeds:
        tr = generate_w7(seed=s, difficulty=DIFF)
        contested = _contested(tr)
        for name, mk in backends:
            for r in range(n_replays):
                rec = _Recorder(mk(r))
                run = run_trace(rec, tr, budget_tokens=BUDGET)
                replay: dict[tuple, BehaviorClass] = {}
                for i, qr in enumerate(run.per_query):
                    pair = contested.get(qr.turn_id)
                    if pair is None:
                        continue
                    earlier, later = pair
                    signaled = rec.conflict_flags[i]      # aligned 1:1, in order
                    replay[(s, qr.turn_id)] = classify_query(
                        earlier, later, qr.retrieved, signaled)
                results[name].append(replay)
    return backends, results


_EXPECTED = {
    "merge":            BehaviorClass.MERGE,
    "last_write_wins":  BehaviorClass.LAST_WRITE_WINS,
    "first_write_wins": BehaviorClass.FIRST_WRITE_WINS,
    "silent_data_loss": BehaviorClass.SILENT_DATA_LOSS,
    "conflict_aware":   BehaviorClass.CONFLICT_FLAG,
    "flaky":            BehaviorClass.NON_DETERMINISTIC,
}


def main(embed_fn=None):
    backends, results = measure(embed_fn)
    n = len(list(SEEDS))
    print(f"\nW7 conflict detection (hard, {n} seeds x {N_REPLAYS} replays, "
          f"stub embedder):\n")
    print(f"  {'backend':16s} {'class':18s} {'unstable':>9s}   distribution")
    print("  " + "-" * 76)
    verdicts: dict[str, BehaviorClass] = {}
    for name, _ in backends:
        cls, summ = classify_backend(results[name])
        verdicts[name] = cls
        dist = ", ".join(f"{k}:{v}" for k, v in summ["distribution"].items()) or "-"
        print(f"  {name:16s} {cls.value:18s} {summ['n_unstable']:9d}   {{{dist}}}")
    print("  " + "-" * 76)
    print("  Each backend lands in exactly one §4.7 class; the conflict_aware "
          "row\n  is the only one reaching conflict_flag (it alone claims "
          "CONFLICT_DETECTION).")

    # Self-check: every toy backend must embody the class it was built for.
    for name, expected in _EXPECTED.items():
        got = verdicts[name]
        assert got is expected, f"{name}: expected {expected.value}, got {got.value}"
    print("\n✓ All six backends classified as designed "
          "(merge / lww / fww / silent / conflict_flag / non_deterministic).")


if __name__ == "__main__":
    main()
