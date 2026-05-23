"""
GRAFOMEM W8 — Forgetting Curve generator (retention policy).

W8 is the retention-axis sibling of W4 (01-workload-spec.md §4.8). W4 pours a
torrent of EQUALLY-important facts and shows that a bounded store evicting by
recency (FIFO, capacity K) cliffs at dependency distance d = K — a structural,
embedder-invariant fact. But W4 cannot distinguish retention POLICIES, because
every fact is equally worth keeping: the only sensible thing to drop is the
oldest. W8 breaks that symmetry.

W8 pours the same long torrent, but facts carry MIXED importance: a sparse set
of HIGH-importance facts (importance 1.0) scattered across the horizon at
log-spaced distances, embedded in a sea of LOW-importance filler (importance
0.1). Every query requires a HIGH-importance fact. Now "what should a bounded
store keep?" has a right answer — keep the important ones — and retention
policies separate:

  - A FIFO store (recency window, capacity K) is importance-blind: a high fact
    at distance d > K is evicted with the filler around it. Recall cliffs at
    d = K, exactly as in W4 — it forgets by age.
  - An importance-weighted store at the SAME capacity K evicts the lowest-
    importance facts first. Because the high facts number <= K, ALL of them
    survive at every distance: recall stays flat at the same footprint. This is
    the paper's open question (limitations: "the only bounded retention strategy
    evaluated is FIFO ... importance-weighted untested") made into a workload —
    principled forgetting is Pareto-dominant on long-horizon recall.
  - An unbounded store keeps everything: flat recall, but footprint grows
    linearly with the horizon (the ceiling, and the footprint foil).

The signal is structural (a high fact is either in the retained window or not),
so it holds for any embedder, like W4's cliff. Distance d is recoverable from
the trace timestamps, so run_w8 bins recall by d with no extra schema.

By construction (the load-bearing invariants):
  - n_high <= K, so importance-weighted retention can keep EVERY queried fact
    while still respecting the same capacity K as FIFO (a fair, equal-footprint
    comparison — otherwise "keep more" would be the trivial answer).
  - high facts span distances straddling K (min < K < max), so the FIFO cliff at
    d = K is visible inside the queried set, not off the end of it.

Retention is not a capability flag (gmp-spec §5; §5.4 "retention is not
deletion"): the backends claim only {AUDIT} and differ by declared policy, so W8
introduces no new Capability. Reuses W4's distinct-entity subject space and the
W1 oracle + validators unchanged (stable facts, one target per query).
Determinism per R1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aml.generator.oracle import derive_ground_truth
from aml.generator.trace import (
    Difficulty,
    Fact,
    Trace,
    Turn,
    TurnRole,
    Workload,
)
from aml.generator.workloads.w1 import (
    _PERSONS,
    _POOLS,
    _PREDICATES,
    _det_uuid,
    _make_rng,
    _question,
    _split_into_sessions,
    _statement,
)
from aml.generator.workloads.w4 import _SURNAMES, _W4_PREDICATES, _log_distances

_HIGH = 1.0
_LOW = 0.1

# The capacity at which the curve is read. Matches W4 / bounded_vector / F9 so
# the FIFO cliff lands at the same structural d = K = 64.
REFERENCE_CAPACITY = 64


@dataclass(frozen=True)
class _W8Params:
    n_facts: int       # horizon length (one intro per turn), as in W4
    n_high: int        # target count of high-importance facts; each is queried.
    n_sessions: int    # must be <= REFERENCE_CAPACITY (importance store keeps all)


_W8_PARAMS = {
    Difficulty.EASY:   _W8Params(n_facts=250,  n_high=40, n_sessions=2),
    Difficulty.MEDIUM: _W8Params(n_facts=1000, n_high=50, n_sessions=5),
    Difficulty.HARD:   _W8Params(n_facts=4000, n_high=50, n_sessions=12),
}


def _high_indices(n_facts: int, n_high: int) -> list[int]:
    """Indices (intro positions) of the high-importance facts, chosen at
    log-spaced distances from the end so they straddle K across the horizon.
    Distinct-distance collisions may yield slightly fewer than n_high."""
    idx = {n_facts - 1 - d for d in _log_distances(n_facts, n_high)}
    return sorted(i for i in idx if 0 <= i < n_facts)


def generate_w8(seed: int, difficulty: Difficulty) -> Trace:
    p = _W8_PARAMS[difficulty]
    rng = _make_rng(Workload.W8, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    high_idx = set(_high_indices(p.n_facts, p.n_high))
    if len(high_idx) > REFERENCE_CAPACITY:
        raise ValueError(
            f"n_high={len(high_idx)} exceeds K={REFERENCE_CAPACITY}; the "
            f"importance store could not keep them all (unfair comparison)")

    # --- subjects: distinct "First Last" entities (reuse W4's space) -------
    subjects = [f"{f} {s}" for f in _PERSONS for s in _SURNAMES]
    rng.shuffle(subjects)
    subjects = subjects[: p.n_facts]

    # --- facts: one per turn; high-importance at the chosen indices --------
    facts: list[Fact] = []
    for i, subj in enumerate(subjects):
        pred = _W4_PREDICATES[rng.randrange(len(_W4_PREDICATES))]
        pool_name, _, _ = _PREDICATES[pred]
        obj = rng.choice(_POOLS[pool_name])
        facts.append(Fact(predicate=pred, subject=subj, object=obj,
                          valid_from=t0 + timedelta(seconds=i),
                          sequence=i + 1,
                          importance=_HIGH if i in high_idx else _LOW))

    # --- turns: introduce the whole torrent, THEN query each high fact -----
    turns: list[Turn] = []
    for i, f in enumerate(facts):
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.USER,
            content=_statement(f), content_template=_statement(f),
            timestamp=t0 + timedelta(seconds=i),
            introduces=[f.fact_id],
        ))
    qbase = p.n_facts
    for qi, i in enumerate(sorted(high_idx)):           # one query per high fact
        f = facts[i]
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
            content=_question(f), content_template=_question(f),
            timestamp=t0 + timedelta(seconds=qbase + qi),
            requires=[f.fact_id], as_of=None,
        ))

    sessions = _split_into_sessions(rng, turns, p.n_sessions)
    result = derive_ground_truth(facts, sessions)

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W8,
        difficulty=difficulty,
        seed=seed,
        facts=result.final_facts,
        sessions=sessions,
        ground_truth=result.ground_truth,
    )


# ============================================================================
# Smoke check — run `python -m aml.generator.workloads.w8`
# ============================================================================

if __name__ == "__main__":
    import hashlib
    import json

    from aml.generator.trace import trace_to_dict
    from aml.generator.validators import validate_trace

    print("GRAFOMEM workloads/w8.py — Forgetting Curve generator\n")

    def _distances(tr):
        intro_ts = {fid: t.timestamp for s in tr.sessions for t in s.turns
                    for fid in t.introduces}
        n = len(tr.facts)
        base = min(intro_ts.values())
        out = {}
        for s in tr.sessions:
            for t in s.turns:
                if t.role == TurnRole.AGENT_QUERY:
                    fid = t.requires[0]
                    i = round((intro_ts[fid] - base).total_seconds())
                    out[t.turn_id] = n - 1 - i
        return out

    # --- Test 1: structure + importance is bimodal ------------------------
    for diff, params in _W8_PARAMS.items():
        tr = generate_w8(seed=0, difficulty=diff)
        imps = {round(f.importance, 3) for f in tr.facts}
        n_high = sum(1 for f in tr.facts if f.importance >= 0.8)
        q = [t for s in tr.sessions for t in s.turns if t.role == TurnRole.AGENT_QUERY]
        assert len(tr.facts) == params.n_facts
        assert imps == {_HIGH, _LOW}, f"{diff.value}: importance not bimodal: {imps}"
        assert n_high <= REFERENCE_CAPACITY, f"{diff.value}: {n_high} high > K"
        assert len(q) == n_high, f"{diff.value}: {len(q)} queries != {n_high} high facts"
        print(f"  {diff.value:7s}: {len(tr.facts):4d} facts "
              f"({n_high:2d} high @1.0, {len(tr.facts)-n_high:4d} low @0.1), "
              f"{len(q):2d} queries (one per high fact)")

    # --- Test 2: queries require ONLY high-importance facts ----------------
    tr = generate_w8(seed=0, difficulty=Difficulty.MEDIUM)
    fact_by_id = {f.fact_id: f for f in tr.facts}
    rt = tr.ground_truth.recall_targets
    q_ids = [t.turn_id for s in tr.sessions for t in s.turns
             if t.role == TurnRole.AGENT_QUERY]
    assert all(len(rt[t]) == 1 for t in q_ids), "each query has exactly one target"
    assert all(fact_by_id[next(iter(rt[t]))].importance >= 0.8 for t in q_ids), \
        "every required fact must be high-importance"
    print("\n✓ Targets high-only     (every query requires an importance>=0.8 fact)")

    # --- Test 3: THE W8 invariant — high facts straddle K ------------------
    dmap = _distances(tr)
    dvals = sorted(dmap.values())
    assert dvals[0] < REFERENCE_CAPACITY < dvals[-1], \
        f"high facts must straddle K={REFERENCE_CAPACITY}: span [{dvals[0]},{dvals[-1]}]"
    n_far = sum(1 for d in dvals if d >= REFERENCE_CAPACITY)
    print(f"✓ Straddle K            (distance span [{dvals[0]}, {dvals[-1]}]; "
          f"{n_far} of {len(dvals)} high facts beyond d=K={REFERENCE_CAPACITY} — "
          f"FIFO must evict them, importance-weighted must keep them)")

    # --- Test 4: validators clean -----------------------------------------
    for diff in _W8_PARAMS:
        v = validate_trace(generate_w8(seed=1, difficulty=diff))
        assert not v, f"{diff.value}: {len(v)} violation(s): {v[:3]}"
    print("✓ Validators clean      (V1-V5 pass on all difficulties)")

    # --- Test 5: determinism ----------------------------------------------
    def chash(tr):
        d = trace_to_dict(tr); d.pop("trace_id", None); d.pop("generated_at", None)
        return hashlib.blake2b(json.dumps(d, sort_keys=True,
                               separators=(",", ":")).encode(), digest_size=16).hexdigest()
    assert chash(generate_w8(seed=2, difficulty=Difficulty.HARD)) == \
           chash(generate_w8(seed=2, difficulty=Difficulty.HARD))
    print("✓ Deterministic (R1)    (hard seed 2 reproduces)")

    print("\nAll W8 smoke checks green. Next: retention backends "
          "(importance-weighted) + run_w8 (recall-by-distance + footprint).")
