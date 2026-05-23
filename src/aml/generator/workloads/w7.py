"""
GRAFOMEM workload W7 — Conflict Detection (deferred design pass, v0.2).

See 01-workload-spec.md §4.7. Where W2 is *clean* drift — one write supersedes
another, non-overlapping windows, a single well-defined current value — W7 is
the pathological case: two writes to the SAME (subject, predicate) whose
validity windows OVERLAP and where NEITHER supersedes the other. Both are
introduced (in two waves, "different threads"), both carry valid_until=None, and
both are therefore simultaneously the "current" value at query time. There is no
correct single answer — that is the point.

Design (why this needs no schema/oracle change):
  - The conflict lives entirely in the FACTS: two same-(s,p) facts, distinct
    objects, distinct valid_from, both open-ended, both superseded_by=None.
    The oracle places BOTH in active_memory at the query (it enforces no
    one-value-per-(s,p) rule); validators stay clean (no supersession chain to
    walk, nothing deleted).
  - The conflict query carries `requires=[A, B]`: both ARE genuinely valid at
    query time, so the oracle's V4 (`req - active == {}`) passes and the
    validator's CONSISTENCY (`recall_targets == requires`) holds. The W7
    *classifier* reads this pair as the contested set; the eval harness must
    dispatch W7 to that classifier rather than a scalar recall scorer (W7
    produces a behavior class, not a recall score — spec §4.7).
  - Ordering is recoverable from `sequence` / `valid_from`: the earlier write is
    the "first" value, the later write the "last". That is what lets a
    classifier separate first_write_wins from last_write_wins.

The behavior classifier (last_write_wins / first_write_wins / merge /
conflict_flag / silent_data_loss / non_deterministic) is the NEXT increment;
this module only produces the deterministic, validator-clean conflict traces it
will consume.

Determinism (R1) is inherited from W1's seeded RNG.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aml.generator.oracle import derive_ground_truth
from aml.generator.trace import (
    Difficulty, Fact, Trace, Turn, TurnRole, Workload,
)
from aml.generator.workloads.w1 import (
    _PERSONS, _POOLS, _PREDICATES,
    _det_uuid, _make_rng, _question, _split_into_sessions, _statement,
)


# ============================================================================
# Difficulty parameters (§4.7)
# ============================================================================

@dataclass(frozen=True, slots=True)
class _W7Params:
    n_units: int        # number of conflicting (subject, predicate) units
    n_sessions: int
    # each unit = two conflicting writes (A, B) + one conflict query.


_W7_PARAMS: dict[Difficulty, _W7Params] = {
    Difficulty.EASY:   _W7Params(n_units=10,  n_sessions=1),
    Difficulty.MEDIUM: _W7Params(n_units=40,  n_sessions=5),
    Difficulty.HARD:   _W7Params(n_units=150, n_sessions=20),
}

_QUERY_GAP = 10        # seconds between the last write and the first query


# ============================================================================
# Generation
# ============================================================================

def _conflict_specs(rng, params: _W7Params):
    """Choose a unique (subject, predicate) per unit and two DISTINCT objects
    for the conflicting writes (avoiding object == subject for person-valued
    predicates)."""
    predicates = list(_PREDICATES.keys())
    pairs = [(s, p) for s in _PERSONS for p in predicates]
    if params.n_units > len(pairs):
        raise ValueError(
            f"need {params.n_units} conflict units, only {len(pairs)} "
            f"(subject, predicate) pairs available"
        )
    rng.shuffle(pairs)

    specs = []
    for subject, predicate in pairs[: params.n_units]:
        pool_name, _, _ = _PREDICATES[predicate]
        candidates = [o for o in _POOLS[pool_name]
                      if not (pool_name == "PERSONS" and o == subject)]
        obj_a, obj_b = rng.sample(candidates, 2)        # two distinct values
        specs.append((subject, predicate, obj_a, obj_b))
    return specs


def generate_w7(seed: int, difficulty: Difficulty) -> Trace:
    params = _W7_PARAMS[difficulty]
    rng = _make_rng(Workload.W7, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    specs = _conflict_specs(rng, params)
    n = len(specs)

    # Two write waves. Wave 1 = the EARLIER value (A) for every unit; wave 2 =
    # the LATER value (B). Both open-ended (valid_until=None), neither
    # superseding the other -> at query time both are simultaneously current.
    # sequence is monotonic across both waves (1..2n) and matches intro order.
    def at(slot: int) -> datetime:
        return t0 + timedelta(seconds=slot)

    facts_a: list[Fact] = []
    facts_b: list[Fact] = []
    seq = 0
    for subject, predicate, obj_a, _obj_b in specs:        # wave 1: A (earlier)
        seq += 1
        facts_a.append(Fact(
            predicate=predicate, subject=subject, object=obj_a,
            valid_from=at(seq - 1), valid_until=None, superseded_by=None,
            sequence=seq, importance=1.0,
        ))
    for subject, predicate, _obj_a, obj_b in specs:        # wave 2: B (later)
        seq += 1
        facts_b.append(Fact(
            predicate=predicate, subject=subject, object=obj_b,
            valid_from=at(seq - 1), valid_until=None, superseded_by=None,
            sequence=seq, importance=1.0,
        ))
    all_facts = facts_a + facts_b
    last_intro_slot = 2 * n - 1

    # --- turns: introduce A wave, then B wave, then conflict queries -------
    turns: list[Turn] = []
    for f in facts_a:
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.USER,
            content=_statement(f), content_template=_statement(f),
            timestamp=f.valid_from, introduces=[f.fact_id],
        ))
    for f in facts_b:
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.USER,
            content=_statement(f), content_template=_statement(f),
            timestamp=f.valid_from, introduces=[f.fact_id],
        ))

    qslot = last_intro_slot + _QUERY_GAP
    for fa, fb in zip(facts_a, facts_b):
        # "What is S's P?" asked in the present. Both writes are valid now;
        # requires=[earlier, later] is the contested set for the classifier.
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
            content=_question(fa), content_template=_question(fa),
            timestamp=at(qslot), requires=[fa.fact_id, fb.fact_id], as_of=None,
        ))
        qslot += 1

    turns.sort(key=lambda t: t.timestamp)
    sessions = _split_into_sessions(rng, turns, params.n_sessions)

    result = derive_ground_truth(all_facts, sessions)

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W7,
        difficulty=difficulty,
        seed=seed,
        facts=result.final_facts,
        sessions=sessions,
        ground_truth=result.ground_truth,
    )


# ============================================================================
# Smoke check — run `python -m aml.generator.workloads.w7`
# ============================================================================

if __name__ == "__main__":
    import time

    from aml.generator.trace import trace_to_dict, validate_trace_schema
    from aml.generator.validators import validate_trace

    print("GRAFOMEM workloads/w7.py — Conflict Detection generator\n")

    def _queries(tr):
        return [t for s in tr.sessions for t in s.turns
                if t.role == TurnRole.AGENT_QUERY]

    # --- Test 1: structure — 2 facts/unit, 1 conflict query/unit ----------
    tr = generate_w7(seed=0, difficulty=Difficulty.EASY)
    qs = _queries(tr)
    assert len(tr.facts) == 20, f"expected 20 facts (10 units x 2), got {len(tr.facts)}"
    assert len(qs) == 10, f"expected 10 conflict queries, got {len(qs)}"
    assert all(len(q.requires) == 2 for q in qs), "every conflict query needs 2 required facts"
    print(f"✓ Generates W7 easy                  "
          f"({len(tr.facts)} facts, {len(qs)} conflict queries, 2 writes each)")

    # --- Test 2: it is a CONFLICT, not a chain (the W7-vs-W2 line) ---------
    # No supersession anywhere; every write open-ended; chains empty.
    assert all(f.superseded_by is None for f in tr.facts), "W7 facts must not supersede"
    assert all(f.valid_until is None for f in tr.facts), "both writes stay current (open-ended)"
    assert tr.ground_truth.superseded_chains == {}, "W7 must have no supersession chains"
    print("✓ Conflict, not drift                "
          "(no superseded_by, all open-ended, 0 supersession chains)")

    # --- Test 3: each query's pair is a real same-(s,p) conflict ----------
    by_id = {f.fact_id: f for f in tr.facts}
    gt = tr.ground_truth
    for q in qs:
        a_id, b_id = q.requires
        fa, fb = by_id[a_id], by_id[b_id]
        assert (fa.subject, fa.predicate) == (fb.subject, fb.predicate), \
            "conflict pair must share (subject, predicate)"
        assert fa.object != fb.object, "conflict pair must disagree on object"
        assert fa.sequence != fb.sequence, "pair needs distinct sequence (first vs last)"
        assert fa.sequence < fb.sequence, "requires=[earlier, later] by convention"
        # CONSISTENCY + V4: recall_targets == requires, and BOTH are live now.
        assert gt.recall_targets[q.turn_id] == {a_id, b_id}
        am = gt.active_memory[q.turn_id]
        assert a_id in am and b_id in am, "both contested writes must be live at query"
    print(f"✓ Genuine contested pairs            "
          f"(same s/p, distinct objects, both live; {len(qs)} queries)")

    # --- Test 4: validators clean -----------------------------------------
    issues = validate_trace(tr)
    assert issues == [], f"W7 easy flagged: {[str(i) for i in issues]}"
    print("✓ Validates clean                    (V1-V5/REF/CONSISTENCY pass)")

    # --- Test 5: determinism (R1) -----------------------------------------
    a = trace_to_dict(generate_w7(seed=7, difficulty=Difficulty.EASY)); a.pop("generated_at")
    b = trace_to_dict(generate_w7(seed=7, difficulty=Difficulty.EASY)); b.pop("generated_at")
    c = trace_to_dict(generate_w7(seed=8, difficulty=Difficulty.EASY)); c.pop("generated_at")
    assert a == b, "same (seed, difficulty) produced different traces"
    assert a != c, "different seeds produced identical traces"
    print("✓ Deterministic across runs (R1)     (seed 7 identical; seed 8 differs)")

    # --- Test 6: schema validation ----------------------------------------
    validate_trace_schema(trace_to_dict(tr))
    print("✓ JSON-Schema validation passed      (conforms to v0.1.3; W7 admitted)")

    # --- Test 7: medium + hard generate and validate clean ----------------
    tm = generate_w7(seed=1, difficulty=Difficulty.MEDIUM)
    assert validate_trace(tm) == [], "W7 medium flagged"
    assert len(tm.facts) == 80 and len(_queries(tm)) == 40
    print(f"✓ Generates + validates W7 medium    ({len(tm.facts)} facts, {len(_queries(tm))} queries)")

    t_start = time.perf_counter()
    th = generate_w7(seed=2, difficulty=Difficulty.HARD)
    elapsed = time.perf_counter() - t_start
    assert validate_trace(th) == [], "W7 hard flagged"
    assert len(th.facts) == 300 and len(_queries(th)) == 150
    validate_trace_schema(trace_to_dict(th))
    print(f"✓ Generates + validates W7 hard      "
          f"({len(th.facts)} facts, {len(_queries(th))} queries, {elapsed:.2f}s)")

    print("\nAll W7 smoke checks green. Conflict traces established; "
          "behavior classifier (last/first_write_wins/merge/...) is next.")
