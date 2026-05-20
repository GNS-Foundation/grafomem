"""
GRAFOMEM W4 — Long-Horizon Dependencies generator.

W4 stresses scale, not retrieval subtlety. A long torrent of unique-entity facts
is introduced, then a sample is queried at controlled *distances from the end*
(d = number of facts introduced AFTER the target). There is no drift, no time
axis, no distractor confusion — every fact is a distinct entity, trivially
unambiguous. The only question is operational: across a long horizon, what does
a backend retain, and can it still answer a dependency d facts back?

The point is the recall-vs-footprint tradeoff over distance:
  - An unbounded store retains everything: recall flat across d, but footprint
    (and per-query scan cost) grows linearly in the horizon.
  - A capacity-bounded store (recency window, capacity K) retains only the last
    K: footprint plateaus at K, but recall cliffs to zero for d >= K (evicted).

The bounded cliff is a *structural* fact (evicted == absent), independent of the
embedder, so it is the clean reproducible signal. Distance d is recoverable from
the trace (query timestamp - target intro timestamp), so run_w4 bins recall by d
without any extra schema. Reuses the W1 oracle and validators unchanged (stable
facts, one target per query). Determinism per R1.

Subjects are "First Last" drawn from W1's 44 first names x 96 surnames (4224
distinct, embedder-friendly entities); predicates are the non-person-valued ones
(objects never collide with subjects).
"""

from __future__ import annotations

import math
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

# 96 surnames -> 44 x 96 = 4224 distinct "First Last" subjects (>= hard horizon).
_SURNAMES = [
    "Adams", "Allen", "Bailey", "Baker", "Barnes", "Bennett", "Brooks", "Bryant",
    "Burns", "Carter", "Chen", "Clark", "Cole", "Collins", "Cooper", "Cox",
    "Cruz", "Diaz", "Dixon", "Edwards", "Evans", "Fisher", "Flores", "Ford",
    "Foster", "Garcia", "Gibson", "Gomez", "Graham", "Grant", "Gray", "Green",
    "Hall", "Hamilton", "Harris", "Hayes", "Henderson", "Hill", "Holmes", "Hughes",
    "Hunt", "Jackson", "James", "Jenkins", "Johnson", "Jones", "Kelly", "Kim",
    "King", "Lee", "Lewis", "Long", "Lopez", "Marshall", "Martin", "Mason",
    "Mendez", "Mills", "Moore", "Morgan", "Morris", "Murphy", "Nelson", "Nguyen",
    "Owens", "Parker", "Patel", "Perez", "Perry", "Phillips", "Price", "Reed",
    "Reyes", "Rice", "Rivera", "Roberts", "Rogers", "Ross", "Russell", "Ryan",
    "Sanders", "Scott", "Shaw", "Simmons", "Stevens", "Stewart", "Sullivan",
    "Torres", "Turner", "Wallace", "Ward", "Watson", "Wells", "West", "Wood",
]

_W4_PREDICATES = [p for p, (pool, _, _) in _PREDICATES.items() if pool != "PERSONS"]


@dataclass(frozen=True)
class _W4Params:
    n_facts: int       # horizon length (one intro per turn)
    n_queries: int     # facts queried, at log-spaced distances from the end
    n_sessions: int


_W4_PARAMS = {
    Difficulty.EASY:   _W4Params(n_facts=250,  n_queries=60,  n_sessions=2),
    Difficulty.MEDIUM: _W4Params(n_facts=1000, n_queries=100, n_sessions=5),
    Difficulty.HARD:   _W4Params(n_facts=4000, n_queries=150, n_sessions=12),
}


def _log_distances(n_facts: int, n_queries: int) -> list[int]:
    """Distinct log-spaced distances in [1, n_facts-1]."""
    hi = n_facts - 1
    ds = set()
    for k in range(n_queries):
        frac = k / max(1, n_queries - 1)
        ds.add(max(1, min(hi, round(hi ** frac))))
    return sorted(ds)


def generate_w4(seed: int, difficulty: Difficulty) -> Trace:
    p = _W4_PARAMS[difficulty]
    rng = _make_rng(Workload.W4, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    # --- subjects: distinct "First Last" entities -------------------------
    subjects = [f"{f} {s}" for f in _PERSONS for s in _SURNAMES]
    rng.shuffle(subjects)
    subjects = subjects[: p.n_facts]

    # --- facts: one per turn, introduced in order (sequence = intro index) -
    facts: list[Fact] = []
    for i, subj in enumerate(subjects):
        pred = _W4_PREDICATES[rng.randrange(len(_W4_PREDICATES))]
        pool_name, _, _ = _PREDICATES[pred]
        obj = rng.choice(_POOLS[pool_name])
        facts.append(Fact(predicate=pred, subject=subj, object=obj,
                          valid_from=t0 + timedelta(seconds=i),
                          sequence=i + 1, importance=1.0))

    # --- turns: introduce the whole torrent, THEN query at distances d ----
    turns: list[Turn] = []
    for i, f in enumerate(facts):
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.USER,
            content=_statement(f), content_template=_statement(f),
            timestamp=t0 + timedelta(seconds=i),
            introduces=[f.fact_id],
        ))

    # query the fact at intro index (n_facts-1-d) for each log-spaced d:
    # d = facts introduced after the target = its distance from the end.
    qbase = p.n_facts
    qi = 0
    for d in _log_distances(p.n_facts, p.n_queries):
        idx = p.n_facts - 1 - d
        if idx < 0:
            continue
        f = facts[idx]
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
            content=_question(f), content_template=_question(f),
            timestamp=t0 + timedelta(seconds=qbase + qi),
            requires=[f.fact_id], as_of=None,
        ))
        qi += 1

    sessions = _split_into_sessions(rng, turns, p.n_sessions)
    result = derive_ground_truth(facts, sessions)

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W4,
        difficulty=difficulty,
        seed=seed,
        facts=result.final_facts,
        sessions=sessions,
        ground_truth=result.ground_truth,
    )


# ============================================================================
# Smoke check — run `python -m aml.generator.workloads.w4`
# ============================================================================

if __name__ == "__main__":
    import hashlib
    import json
    import time

    from aml.generator.trace import trace_to_dict
    from aml.generator.validators import validate_trace

    print("GRAFOMEM workloads/w4.py — Long-Horizon Dependencies generator\n")

    def distances(tr: Trace) -> list[int]:
        # recover d = (target intro index from end) from timestamps
        intro_ts = {}
        for s in tr.sessions:
            for t in s.turns:
                for fid in t.introduces:
                    intro_ts[fid] = t.timestamp
        n = len(tr.facts)
        base = min(ts for ts in intro_ts.values())
        out = []
        for s in tr.sessions:
            for t in s.turns:
                if t.role == TurnRole.AGENT_QUERY:
                    fid = t.requires[0]
                    i = round((intro_ts[fid] - base).total_seconds())
                    out.append(n - 1 - i)
        return out

    # --- Test 1: structure + distance coverage ----------------------------
    for diff, params in _W4_PARAMS.items():
        tr = generate_w4(seed=0, difficulty=diff)
        ds = distances(tr)
        assert len(tr.facts) == params.n_facts
        assert all(d >= 0 for d in ds)
        print(f"  {diff.value:7s}: {len(tr.facts):4d} facts, {len(ds):3d} queries, "
              f"distance d in [{min(ds)}, {max(ds)}] (log-spaced)")

    # --- Test 2: oracle agreement (W1 oracle, unchanged) ------------------
    tr = generate_w4(seed=0, difficulty=Difficulty.MEDIUM)
    rt = tr.ground_truth.recall_targets
    q_ids = [t.turn_id for s in tr.sessions for t in s.turns
             if t.role == TurnRole.AGENT_QUERY]
    assert len(rt) == len(q_ids) and all(len(rt[t]) == 1 for t in q_ids)
    print(f"\n✓ Oracle agreement      ({len(q_ids)} queries, one target each)")

    # --- Test 3: distance is log-spaced and spans the horizon -------------
    ds = sorted(distances(generate_w4(seed=0, difficulty=Difficulty.HARD)))
    assert ds[0] <= 4 and ds[-1] >= 2000, f"distance span too narrow: [{ds[0]},{ds[-1]}]"
    print(f"✓ Distance span (hard)  (d from {ds[0]} to {ds[-1]}; "
          f"a capacity-K backend will cliff at d=K)")

    # --- Test 4: validators pass ------------------------------------------
    for diff in _W4_PARAMS:
        v = validate_trace(generate_w4(seed=1, difficulty=diff))
        assert not v, f"{diff.value}: {len(v)} violation(s): {v[:3]}"
    print("✓ Validators clean      (V1-V5 pass on all difficulties)")

    # --- Test 5: determinism (R1) -----------------------------------------
    def chash(tr):
        d = trace_to_dict(tr); d.pop("trace_id", None); d.pop("generated_at", None)
        return hashlib.blake2b(json.dumps(d, sort_keys=True,
                               separators=(",", ":")).encode(), digest_size=16).hexdigest()
    assert chash(generate_w4(seed=2, difficulty=Difficulty.HARD)) == \
           chash(generate_w4(seed=2, difficulty=Difficulty.HARD))
    print("✓ Deterministic (R1)    (hard seed 2 reproduces)")

    # --- Test 6: perf at hard (the oracle is the thing to watch) ----------
    t = time.perf_counter()
    generate_w4(seed=0, difficulty=Difficulty.HARD)
    dt = time.perf_counter() - t
    print(f"\nAll W4 smoke checks green. (hard, {_W4_PARAMS[Difficulty.HARD].n_facts} "
          f"facts, generates in {dt:.2f}s)")
    print("Next: bounded_vector backend (FIFO eviction) + run_w4 (recall-by-distance + footprint).")
