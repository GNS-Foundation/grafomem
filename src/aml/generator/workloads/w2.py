"""
GRAFOMEM workload W2 — Drift & Conflict (supersession).

See 01-workload-spec.md §4.2. Purpose: test whether a backend tracks the
temporal drift of a fact. Each chain is a sequence of facts about the same
(subject, predicate) whose object changes over time: F0 -> F1 -> ... -> Fk,
each superseding the last. F_i is valid on [t_i, t_{i+1}); the head F_k is
valid on [t_k, infinity).

Two query kinds, both asked from the present (transaction-time = now, after all
introductions):
  - CURRENT  (as_of = None): "what is S's P now?"  -> requires the chain HEAD.
  - HISTORICAL (as_of = t in a past version's window): "...previously?"
                                                       -> requires that version.

Capability gating (matrix, doc 02 §7): historical (as_of) queries require
BI_TEMPORAL; a backend without it has those queries excluded from its Q_W
(the runner handles this). A SUPERSESSION_CHAIN backend should also keep the
superseded versions out of "current" results — the precision story.

The supersession linkage lives in the FACTS (superseded_by + valid_until), so
the runner derives supersede-vs-write dispatch from the fact set; no Turn
schema change is needed. Determinism (R1) is inherited from W1's seeded RNG.

Reuses W1's synthetic vocab + helpers (a shared _common module is the eventual
home; for now W1 is the single source of the entity population).
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
# Difficulty parameters (§4.2)
# ============================================================================

@dataclass(frozen=True, slots=True)
class _W2Params:
    n_chains: int
    depth_range: tuple[int, int]      # inclusive [min, max] versions per chain
    n_sessions: int


_W2_PARAMS: dict[Difficulty, _W2Params] = {
    Difficulty.EASY:   _W2Params(n_chains=10,  depth_range=(2, 2), n_sessions=1),
    Difficulty.MEDIUM: _W2Params(n_chains=40,  depth_range=(2, 3), n_sessions=5),
    Difficulty.HARD:   _W2Params(n_chains=150, depth_range=(2, 5), n_sessions=20),
}

_QUERY_GAP = 10        # seconds between last introduction and first query


# ============================================================================
# Generation
# ============================================================================

def _chain_specs(rng, params):
    """Choose unique (subject, predicate) per chain, a depth, and that many
    distinct objects (so each version differs)."""
    predicates = list(_PREDICATES.keys())
    pairs = [(s, p) for s in _PERSONS for p in predicates]
    if params.n_chains > len(pairs):
        raise ValueError(f"need {params.n_chains} chains, only {len(pairs)} pairs")
    rng.shuffle(pairs)

    specs = []
    for subject, predicate in pairs[:params.n_chains]:
        depth = rng.randint(*params.depth_range)
        pool_name, _, _ = _PREDICATES[predicate]
        candidates = [o for o in _POOLS[pool_name]
                      if not (pool_name == "PERSONS" and o == subject)]
        objects = rng.sample(candidates, depth)        # distinct per version
        specs.append((subject, predicate, objects))
    return specs


def generate_w2(seed: int, difficulty: Difficulty) -> Trace:
    params = _W2_PARAMS[difficulty]
    rng = _make_rng(Workload.W2, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    specs = _chain_specs(rng, params)
    max_depth = max(len(objs) for _, _, objs in specs)

    # --- assign introduction times in waves: all F0s, then all F1s, ... ----
    intro_time: dict[tuple[int, int], datetime] = {}
    slot = 0
    for version in range(max_depth):
        for ci, (_s, _p, objs) in enumerate(specs):
            if version < len(objs):
                intro_time[(ci, version)] = t0 + timedelta(seconds=slot)
                slot += 1
    last_intro_slot = slot - 1

    # sequence numbers follow introduction (slot) order
    slot_order = sorted(intro_time, key=lambda k: intro_time[k])
    seq_of = {k: i + 1 for i, k in enumerate(slot_order)}

    # --- build facts head-first per chain so superseded_by can link forward -
    chain_facts: dict[int, list[Fact]] = {}
    for ci, (subject, predicate, objects) in enumerate(specs):
        d = len(objects)
        facts: list[Fact | None] = [None] * d
        next_fid: bytes | None = None
        for version in reversed(range(d)):
            valid_from = intro_time[(ci, version)]
            valid_until = intro_time[(ci, version + 1)] if version + 1 < d else None
            f = Fact(
                predicate=predicate, subject=subject, object=objects[version],
                valid_from=valid_from, valid_until=valid_until,
                superseded_by=next_fid, sequence=seq_of[(ci, version)],
                importance=1.0,
            )
            facts[version] = f
            next_fid = f.fact_id
        chain_facts[ci] = facts  # type: ignore[assignment]

    all_facts = [f for ci in range(len(specs)) for f in chain_facts[ci]]

    # --- introduction turns (one per version, at its valid_from) ------------
    turns: list[Turn] = []
    for ci, (_s, _p, objs) in enumerate(specs):
        for version in range(len(objs)):
            f = chain_facts[ci][version]
            turns.append(Turn(
                turn_id=_det_uuid(rng), role=TurnRole.USER,
                content=_statement(f), content_template=_statement(f),
                timestamp=intro_time[(ci, version)], introduces=[f.fact_id],
            ))

    # --- query turns (all from the present, after every introduction) -------
    qslot = last_intro_slot + _QUERY_GAP
    for ci, (_s, _p, objs) in enumerate(specs):
        facts = chain_facts[ci]
        head = facts[-1]
        # CURRENT: requires the head (valid now).
        turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
            content=_question(head), content_template=_question(head),
            timestamp=t0 + timedelta(seconds=qslot),
            requires=[head.fact_id], as_of=None,
        ))
        qslot += 1
        # HISTORICAL: one per superseded version, as_of inside its window.
        for version in range(len(facts) - 1):
            f = facts[version]
            t_lo = intro_time[(ci, version)]
            t_hi = intro_time[(ci, version + 1)]
            as_of = t_lo + (t_hi - t_lo) / 2          # strictly inside [t_lo,t_hi)
            content = _question(f) + " (previously)"
            turns.append(Turn(
                turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
                content=content, content_template=content,
                timestamp=t0 + timedelta(seconds=qslot),
                requires=[f.fact_id], as_of=as_of,
            ))
            qslot += 1

    turns.sort(key=lambda t: t.timestamp)
    sessions = _split_into_sessions(rng, turns, params.n_sessions)

    result = derive_ground_truth(all_facts, sessions)

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W2,
        difficulty=difficulty,
        seed=seed,
        facts=result.final_facts,
        sessions=sessions,
        ground_truth=result.ground_truth,
    )


# ============================================================================
# Smoke check — run `python -m aml.generator.workloads.w2`
# ============================================================================

if __name__ == "__main__":
    import time

    from aml.generator.trace import trace_to_dict, validate_trace_schema
    from aml.generator.validators import validate_trace

    print("GRAFOMEM workloads/w2.py — Drift & Conflict generator\n")

    def _counts(tr):
        cur = hist = 0
        for s in tr.sessions:
            for t in s.turns:
                if t.role == TurnRole.AGENT_QUERY:
                    if t.as_of is None:
                        cur += 1
                    else:
                        hist += 1
        return len(tr.facts), cur, hist

    # --- Test 1: easy structure + supersession present --------------------
    tr = generate_w2(seed=0, difficulty=Difficulty.EASY)
    nf, ncur, nhist = _counts(tr)
    assert nf == 20, f"expected 20 facts (10 chains x depth 2), got {nf}"
    assert ncur == 10 and nhist == 10, f"expected 10 current + 10 historical, got {ncur}+{nhist}"
    n_super = sum(1 for f in tr.facts if f.superseded_by is not None)
    assert n_super == 10, f"expected 10 superseded facts, got {n_super}"
    print(f"✓ Generates W2 easy                  "
          f"({nf} facts, {ncur} current + {nhist} historical q, "
          f"{n_super} supersessions)")

    # --- Test 2: validates clean (oracle + independent validator agree) ---
    issues = validate_trace(tr)
    assert issues == [], f"W2 easy flagged: {[str(i) for i in issues]}"
    print("✓ Validates clean                    "
          "(bi-temporal V4 holds; chains repair-clean)")

    # --- Test 3: bi-temporal ground truth is correct ----------------------
    # A current query targets the head; the matching historical query targets
    # the original (pre-supersession) version of the SAME (subject,predicate).
    by_turn = {t.turn_id: t for s in tr.sessions for t in s.turns}
    gt = tr.ground_truth
    checked = 0
    for tid, turn in by_turn.items():
        if turn.role != TurnRole.AGENT_QUERY:
            continue
        rt = gt.recall_targets[tid]
        assert rt == set(turn.requires), "recall_target != requires"
        [fid] = list(rt)
        fact = next(f for f in tr.facts if f.fact_id == fid)
        if turn.as_of is None:
            assert fact.superseded_by is None, "current query target is not the head"
        else:
            assert fact.valid_from <= turn.as_of < (fact.valid_until or fact.valid_from + timedelta(days=1)), \
                "historical target not valid at as_of"
            assert fact.superseded_by is not None, "historical query target is the head"
        checked += 1
    print(f"✓ Bi-temporal targets correct        "
          f"(current->head, historical->as-of-valid version; {checked} queries)")

    # --- Test 4: determinism (R1) -----------------------------------------
    a = trace_to_dict(generate_w2(seed=7, difficulty=Difficulty.EASY))
    b = trace_to_dict(generate_w2(seed=7, difficulty=Difficulty.EASY))
    a.pop("generated_at"); b.pop("generated_at")
    assert a == b, "same (seed, difficulty) produced different traces"
    c = trace_to_dict(generate_w2(seed=8, difficulty=Difficulty.EASY))
    c.pop("generated_at")
    assert a != c, "different seeds produced identical traces"
    print("✓ Deterministic across runs (R1)     (seed 7 identical; seed 8 differs)")

    # --- Test 5: schema validation ----------------------------------------
    validate_trace_schema(trace_to_dict(tr))
    print("✓ JSON-Schema validation passed      (conforms to v0.1.3)")

    # --- Test 6: medium + hard generate and validate clean ----------------
    tm = generate_w2(seed=1, difficulty=Difficulty.MEDIUM)
    assert validate_trace(tm) == [], "W2 medium flagged"
    mf, mcur, mhist = _counts(tm)
    print(f"✓ Generates + validates W2 medium    "
          f"({mf} facts, {mcur} current + {mhist} historical q)")

    t_start = time.perf_counter()
    th = generate_w2(seed=2, difficulty=Difficulty.HARD)
    elapsed = time.perf_counter() - t_start
    assert validate_trace(th) == [], "W2 hard flagged"
    hf, hcur, hhist = _counts(th)
    validate_trace_schema(trace_to_dict(th))
    print(f"✓ Generates + validates W2 hard      "
          f"({hf} facts, {hcur} current + {hhist} historical q, {elapsed:.2f}s)")

    print("\nAll W2 smoke checks green. Supersession + bi-temporal ground truth "
          "established; runner supersede-dispatch is next.")
