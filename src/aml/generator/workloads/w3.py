"""
GRAFOMEM W3 — Distractor Noise generator.

W3 is structurally W1 (stable, unique-(subject,predicate) facts, current-state
queries) plus a flood of never-queried distractor facts. There is no drift and
no time axis, so the drift capabilities (SUPERSESSION_CHAIN, BI_TEMPORAL) buy
nothing here: W3 isolates *semantic discrimination* — can a backend return the
queried fact without dragging in plausible look-alikes? M2 precision is the
headline; recall@1 at a tight budget is the discrimination metric (does the
signal outrank its near-misses?).

Three kinds of fact:

  - SIGNAL    (queried): unique (subject, predicate) drawn from predicate groups
                with >= 2 siblings (so confusable near-misses exist). The target
                of exactly one query.
  - NEAR-MISS (distractor): same subject + sibling predicate (same object pool)
                as a signal — e.g. signal "Alice lives in Rome", near-miss
                "Alice was born in Paris". Same person, same object type, only
                the relation differs. The hard confusables.
  - VOLUME    (distractor): any other unique (subject, predicate). Easy to
                reject; bulks up the haystack to stress precision under volume.

All facts are introduced before any query, so the full haystack is present at
retrieval time. The W1 oracle derives ground truth unchanged: each signal query
requires its one signal fact; distractors appear in no recall_target, so
returning one is a precision miss. Determinism per R1 (seeded RNG, stable
cross-machine hash).
"""

from __future__ import annotations

from collections import defaultdict
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

# Sibling predicate groups (predicates sharing an object pool). Signals are
# drawn only from groups with >= 2 members, so each signal has at least one
# confusable near-miss (same subject, same object type, different relation).
_GROUPS: dict[str, list[str]] = defaultdict(list)
for _p, (_pool, _, _) in _PREDICATES.items():
    _GROUPS[_pool].append(_p)
_SIGNAL_GROUPS = {pool: preds for pool, preds in _GROUPS.items() if len(preds) >= 2}
_SIGNAL_PREDICATES = [p for preds in _SIGNAL_GROUPS.values() for p in preds]
_GROUP_OF = {p: pool for pool, preds in _GROUPS.items() for p in preds}


@dataclass(frozen=True)
class _W3Params:
    n_signal: int            # queried facts
    nearmiss_per_signal: int # confusable siblings per signal (capped by group)
    n_volume: int            # unrelated distractors
    n_sessions: int


_W3_PARAMS = {
    Difficulty.EASY:   _W3Params(n_signal=10, nearmiss_per_signal=1, n_volume=10, n_sessions=1),
    Difficulty.MEDIUM: _W3Params(n_signal=40, nearmiss_per_signal=2, n_volume=80, n_sessions=4),
    Difficulty.HARD:   _W3Params(n_signal=80, nearmiss_per_signal=3, n_volume=360, n_sessions=12),
}


def _obj_for(rng, predicate: str, subject: str):
    pool_name, _, _ = _PREDICATES[predicate]
    pool = _POOLS[pool_name]
    obj = rng.choice(pool)
    if pool_name == "PERSONS":           # person-valued predicate: avoid o == s
        while obj == subject:
            obj = rng.choice(pool)
    return obj


def generate_w3(seed: int, difficulty: Difficulty) -> Trace:
    p = _W3_PARAMS[difficulty]
    rng = _make_rng(Workload.W3, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    used: set[tuple[str, str]] = set()        # global (subject, predicate) uniqueness
    # specs: (subject, predicate, is_signal) — selected before objects/timestamps
    specs: list[tuple[str, str, bool]] = []

    # --- signals (+ their near-miss siblings) -----------------------------
    signal_pairs = [(s, pr) for s in _PERSONS for pr in _SIGNAL_PREDICATES]
    rng.shuffle(signal_pairs)
    n_sig = 0
    for (s, pr) in signal_pairs:
        if n_sig >= p.n_signal:
            break
        if (s, pr) in used:
            continue
        used.add((s, pr))
        specs.append((s, pr, True))
        n_sig += 1
        siblings = [q for q in _SIGNAL_GROUPS[_GROUP_OF[pr]] if q != pr]
        rng.shuffle(siblings)
        added = 0
        for q in siblings:
            if added >= p.nearmiss_per_signal:
                break
            if (s, q) in used:
                continue
            used.add((s, q))
            specs.append((s, q, False))     # near-miss distractor
            added += 1

    # --- volume distractors: any other unique (S,P), never queried --------
    volume_pairs = [(s, pr) for s in _PERSONS for pr in _PREDICATES]
    rng.shuffle(volume_pairs)
    n_vol = 0
    for (s, pr) in volume_pairs:
        if n_vol >= p.n_volume:
            break
        if (s, pr) in used:
            continue
        used.add((s, pr))
        specs.append((s, pr, False))
        n_vol += 1

    # --- materialize facts in shuffled intro order (sequence = intro order) -
    rng.shuffle(specs)
    facts: list[Fact] = []
    signal_ids: set[bytes] = set()
    for i, (s, pr, is_sig) in enumerate(specs):
        f = Fact(predicate=pr, subject=s, object=_obj_for(rng, pr, s),
                 valid_from=t0, sequence=i + 1, importance=1.0)
        facts.append(f)
        if is_sig:
            signal_ids.add(f.fact_id)

    # --- turns: introduce the whole haystack, THEN query the signals ------
    turns: list[Turn] = []
    k = 0
    for f in facts:
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.USER,
            content=_statement(f), content_template=_statement(f),
            timestamp=t0 + timedelta(seconds=k),
            introduces=[f.fact_id],
        ))
        k += 1
    query_facts = [f for f in facts if f.fact_id in signal_ids]
    rng.shuffle(query_facts)
    for f in query_facts:
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
            content=_question(f), content_template=_question(f),
            timestamp=t0 + timedelta(seconds=k),
            requires=[f.fact_id], as_of=None,
        ))
        k += 1

    sessions = _split_into_sessions(rng, turns, p.n_sessions)
    result = derive_ground_truth(facts, sessions)

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W3,
        difficulty=difficulty,
        seed=seed,
        facts=result.final_facts,
        sessions=sessions,
        ground_truth=result.ground_truth,
    )


# ============================================================================
# Smoke check — run `python -m aml.generator.workloads.w3`
# ============================================================================

if __name__ == "__main__":
    import time

    from aml.generator.trace import trace_to_dict
    from aml.generator.validators import validate_trace

    print("GRAFOMEM workloads/w3.py — Distractor Noise generator\n")

    def stats(tr: Trace):
        nq = sum(1 for s in tr.sessions for t in s.turns
                 if t.role == TurnRole.AGENT_QUERY)
        return len(tr.facts), nq

    # --- Test 1: structure across difficulties ----------------------------
    for diff, params in _W3_PARAMS.items():
        tr = generate_w3(seed=0, difficulty=diff)
        nf, nq = stats(tr)
        assert nq == params.n_signal, f"{diff.value}: {nq} queries != {params.n_signal} signal"
        # distractors = everything not queried; must dominate at higher difficulty
        n_distractor = nf - nq
        assert n_distractor >= params.n_volume, f"{diff.value}: too few distractors"
        ratio = n_distractor / nq
        print(f"  {diff.value:7s}: {nf:4d} facts = {nq:3d} signal + "
              f"{n_distractor:4d} distractor   (haystack {ratio:.1f}x signal)")

    # --- Test 2: oracle agreement (W1 oracle, unchanged) ------------------
    tr = generate_w3(seed=0, difficulty=Difficulty.MEDIUM)
    rt = tr.ground_truth.recall_targets
    q_turn_ids = [t.turn_id for s in tr.sessions for t in s.turns
                  if t.role == TurnRole.AGENT_QUERY]
    assert len(rt) == len(q_turn_ids), "every query must have a recall target"
    # each target is exactly one fact, and it's a signal (queried) fact
    queried_fids = {next(iter(rt[tid])) for tid in q_turn_ids}
    assert all(len(rt[tid]) == 1 for tid in q_turn_ids), "targets must be singletons"
    # distractors appear in NO recall target (the precision trap)
    all_fids = {f.fact_id for f in tr.facts}
    target_fids = set().union(*rt.values())
    distractor_fids = all_fids - target_fids
    assert len(distractor_fids) == len(tr.facts) - len(queried_fids)
    print(f"\n✓ Oracle agreement      ({len(q_turn_ids)} signal queries, "
          f"{len(distractor_fids)} distractors in no recall_target)")

    # --- Test 3: near-miss structure (same subject + object pool) ---------
    by_subj_pool = defaultdict(list)
    fact_by_id = {f.fact_id: f for f in tr.facts}
    for tid in q_turn_ids:
        sig = fact_by_id[next(iter(rt[tid]))]
        pool = _PREDICATES[sig.predicate][0]
        siblings = [f for f in tr.facts
                    if f.subject == sig.subject and f.fact_id != sig.fact_id
                    and _PREDICATES[f.predicate][0] == pool]
        by_subj_pool[sig.fact_id] = siblings
    n_with_nearmiss = sum(1 for v in by_subj_pool.values() if v)
    print(f"✓ Near-miss structure   ({n_with_nearmiss}/{len(q_turn_ids)} signals have a "
          f"same-subject same-pool sibling)")

    # --- Test 4: independent validators pass ------------------------------
    for diff in _W3_PARAMS:
        violations = validate_trace(generate_w3(seed=1, difficulty=diff))
        assert not violations, f"{diff.value}: {len(violations)} violation(s): {violations[:3]}"
    print("✓ Validators clean      (V1-V5 pass on all difficulties)")

    # --- Test 5: determinism (R1) -----------------------------------------
    import hashlib
    import json
    def chash(tr):
        d = trace_to_dict(tr)
        d.pop("trace_id", None); d.pop("generated_at", None)
        return hashlib.blake2b(
            json.dumps(d, sort_keys=True, separators=(",", ":")).encode(),
            digest_size=16).hexdigest()
    a = chash(generate_w3(seed=2, difficulty=Difficulty.HARD))
    b = chash(generate_w3(seed=2, difficulty=Difficulty.HARD))
    assert a == b, "non-deterministic generation"
    print(f"✓ Deterministic (R1)    (hard seed 2 reproduces: {a[:16]}...)")

    t = time.perf_counter()
    generate_w3(seed=0, difficulty=Difficulty.HARD)
    print(f"\nAll W3 smoke checks green. (hard generates in "
          f"{time.perf_counter()-t:.2f}s) Next: add W3 to corpus + run_w3 precision experiment.")
