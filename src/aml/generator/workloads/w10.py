"""
GRAFOMEM workload W10 — Concurrency & Isolation (§4.10).

The frontier workload. W1-W9 have a single, total-order ground truth; W10 does
not. Its core is a set of CONCURRENCY GROUPS: 2-3 transactions contending on a
key, with one planted anomaly each. The permissible outcomes depend on the
isolation level the BACKEND declares (§10.2), so the ground truth is computed at
eval time by aml.eval.concurrency — NOT pre-baked here.

Structure of a W10 trace:
  - A sequential PREFIX of `txn_id=None` USER turns that introduces the base
    fact (the v0 value) of every contended key, plus one current-query probe
    per group. The prefix is single-valued and goes through derive_ground_truth
    exactly like W1-W9 — that is the ONLY thing the oracle sees here.
  - One CONCURRENCY GROUP per probe. Each group's ops live in `txn_id`-tagged
    turns (Transaction carries only the happens-before DAG, not ops); the
    contending transactions are concurrent (empty depends_on). These turns are
    held OUT of the derive_ground_truth call so the oracle never tries to
    linearise genuinely-concurrent writes.

Planted anomalies (TxnAnomaly), each a textbook pattern (§10.2):
  - LOST_UPDATE         two transactions supersede ONE key (v0 -> a, v0 -> b).
  - WRITE_SKEW          two transactions supersede TWO keys; the joint invariant
                        is violated only if both commit. The group carries key_a
                        as its (subject, predicate); key_b is read from T2's turn.
  - NON_REPEATABLE_READ a reader transaction reads one key twice, concurrent with
                        a writer that supersedes it. The reads are set-valued
                        (requires=[]); the read key is the group's (subject,
                        predicate).

The concurrent write-facts carry `superseded_by=None`: the supersession linkage
is the contention the backend resolves, unlike W2 where it is predetermined.

Reuses W1 vocab/templates + the W1 oracle. Determinism per R1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aml.generator.oracle import derive_ground_truth
from aml.generator.trace import (
    ConcurrencyGroup,
    Difficulty,
    Fact,
    SCHEMA_VERSION_V2,
    Session,
    Trace,
    Transaction,
    Turn,
    TurnRole,
    TxnAnomaly,
    Workload,
)
from aml.generator.workloads.w1 import (
    _PERSONS,
    _POOLS,
    _PREDICATES,
    _det_uuid,
    _make_rng,
    _question,
    _statement,
)


# ============================================================================
# Difficulty parameters (§4.10): how many groups of each anomaly
# ============================================================================

@dataclass(frozen=True, slots=True)
class _W10Params:
    n_lost: int
    n_skew: int
    n_nrr: int


_W10_PARAMS: dict[Difficulty, _W10Params] = {
    Difficulty.EASY:   _W10Params(n_lost=1, n_skew=1, n_nrr=1),
    Difficulty.MEDIUM: _W10Params(n_lost=3, n_skew=3, n_nrr=3),
    Difficulty.HARD:   _W10Params(n_lost=8, n_skew=8, n_nrr=8),
}


# ============================================================================
# Generation
# ============================================================================

def generate_w10(seed: int, difficulty: Difficulty) -> Trace:
    params = _W10_PARAMS[difficulty]
    rng = _make_rng(Workload.W10, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def at(s: int) -> datetime:
        return t0 + timedelta(seconds=s)

    # Distinct (subject, predicate) keys, drawn without replacement.
    keys = [(s, p) for s in _PERSONS for p in _PREDICATES]
    rng.shuffle(keys)
    key_iter = iter(keys)

    def objects_for(subject: str, predicate: str, k: int) -> list[str]:
        pool_name = _PREDICATES[predicate][0]
        pool = [o for o in _POOLS[pool_name]
                if not (pool_name == "PERSONS" and o == subject)]
        return rng.sample(pool, k)

    # --- Plan each group's parameters (values drawn now; turns built below) -
    plans: list[dict] = []
    for _ in range(params.n_lost):
        s, p = next(key_iter)
        v0, a, b = objects_for(s, p, 3)
        plans.append({"kind": TxnAnomaly.LOST_UPDATE, "key": (s, p),
                      "base": v0, "writes": [a, b]})
    for _ in range(params.n_skew):
        sa, pa = next(key_iter)
        sb, pb = next(key_iter)
        ba, na = objects_for(sa, pa, 2)
        bb, nb = objects_for(sb, pb, 2)
        plans.append({"kind": TxnAnomaly.WRITE_SKEW,
                      "key_a": (sa, pa), "base_a": ba, "new_a": na,
                      "key_b": (sb, pb), "base_b": bb, "new_b": nb})
    for _ in range(params.n_nrr):
        s, p = next(key_iter)
        v0, b = objects_for(s, p, 2)
        plans.append({"kind": TxnAnomaly.NON_REPEATABLE_READ, "key": (s, p),
                      "base": v0, "new": b})
    rng.shuffle(plans)  # interleave anomaly types across the trace

    seq = 0  # strictly-monotonic fact sequence (>= 1)

    def mk_fact(subject, predicate, obj, valid_from) -> Fact:
        nonlocal seq
        seq += 1
        return Fact(predicate=predicate, subject=subject, object=obj,
                    valid_from=valid_from, sequence=seq, importance=1.0)

    # --- PHASE 1: base facts (the v0 of every contended key) ---------------
    base_facts: list[Fact] = []
    base_by_key: dict[tuple[str, str], Fact] = {}

    def add_base(subject, predicate, obj) -> Fact:
        f = mk_fact(subject, predicate, obj, t0)
        base_facts.append(f)
        base_by_key[(subject, predicate)] = f
        return f

    for gp in plans:
        if gp["kind"] is TxnAnomaly.WRITE_SKEW:
            add_base(*gp["key_a"], gp["base_a"])
            add_base(*gp["key_b"], gp["base_b"])
        else:
            add_base(*gp["key"], gp["base"])

    # base introduction turns (one per base fact), at the front of the timeline
    base_turns: list[Turn] = []
    for i, f in enumerate(base_facts):
        base_turns.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.USER,
            content=_statement(f), content_template=_statement(f),
            timestamp=at(i), introduces=[f.fact_id]))
    n_base = len(base_facts)

    # one CURRENT-query probe per group, after all base intros (single-valued)
    prefix_queries: list[Turn] = []
    qslot = n_base
    for gp in plans:
        key = gp["key_a"] if gp["kind"] is TxnAnomaly.WRITE_SKEW else gp["key"]
        f0 = base_by_key[key]
        prefix_queries.append(Turn(
            turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
            content=_question(f0), content_template=_question(f0),
            timestamp=at(qslot), requires=[f0.fact_id], as_of=None))
        qslot += 1

    prefix_turns = base_turns + prefix_queries
    prefix_session = Session(
        session_id=_det_uuid(rng),
        start_time=prefix_turns[0].timestamp,
        end_time=prefix_turns[-1].timestamp,
        turns=prefix_turns, tenant_id=None)

    # --- PHASE 2: concurrency groups (txn-tagged turns + DAG) --------------
    write_facts: list[Fact] = []
    groups: list[ConcurrencyGroup] = []
    group_sessions: list[Session] = []
    cslot = qslot  # concurrent phase begins after the prefix queries

    for gp in plans:
        tc = at(cslot)
        cslot += 1
        kind = gp["kind"]
        gturns: list[Turn] = []

        if kind is TxnAnomaly.LOST_UPDATE:
            s, p = gp["key"]
            a, b = gp["writes"]
            t1, t2 = _det_uuid(rng), _det_uuid(rng)
            for tid, val in ((t1, a), (t2, b)):
                f = mk_fact(s, p, val, tc)
                write_facts.append(f)
                gturns.append(Turn(
                    turn_id=_det_uuid(rng), role=TurnRole.USER,
                    content=_statement(f), content_template=_statement(f),
                    timestamp=tc, introduces=[f.fact_id], txn_id=tid))
            transactions = [Transaction(t1), Transaction(t2)]  # concurrent
            group = ConcurrencyGroup(subject=s, predicate=p,
                                     anomaly=kind, transactions=transactions)

        elif kind is TxnAnomaly.WRITE_SKEW:
            sa, pa = gp["key_a"]
            sb, pb = gp["key_b"]
            base_a = base_by_key[(sa, pa)]
            base_b = base_by_key[(sb, pb)]
            t1, t2 = _det_uuid(rng), _det_uuid(rng)
            fa = mk_fact(sa, pa, gp["new_a"], tc)
            fb = mk_fact(sb, pb, gp["new_b"], tc)
            write_facts += [fa, fb]
            # Each writer READS the OTHER writer's key before superseding its own.
            # That rw-antidependency cycle (T1 read key_b that T2 writes; T2 read
            # key_a that T1 writes) is what lets a serializable (SSI) store abort
            # one writer and avoid the skew. Without these reads, two unconditional
            # writes to disjoint keys are anomaly-free and serializable cannot be
            # told from snapshot. requires=[other base] names the read KEY (a
            # set-valued read, safe via the validator's txn_id skip — not a target).
            gturns.append(Turn(
                turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
                content=f"{_question(base_b)} (read)", content_template=_question(base_b),
                timestamp=tc, requires=[base_b.fact_id], as_of=None, txn_id=t1))
            gturns.append(Turn(
                turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
                content=f"{_question(base_a)} (read)", content_template=_question(base_a),
                timestamp=tc, requires=[base_a.fact_id], as_of=None, txn_id=t2))
            gturns.append(Turn(
                turn_id=_det_uuid(rng), role=TurnRole.USER,
                content=_statement(fa), content_template=_statement(fa),
                timestamp=tc, introduces=[fa.fact_id], txn_id=t1))
            gturns.append(Turn(
                turn_id=_det_uuid(rng), role=TurnRole.USER,
                content=_statement(fb), content_template=_statement(fb),
                timestamp=tc, introduces=[fb.fact_id], txn_id=t2))
            transactions = [Transaction(t1), Transaction(t2)]  # concurrent
            # key_a is the group's (subject, predicate); key_b is read from T2.
            group = ConcurrencyGroup(subject=sa, predicate=pa,
                                     anomaly=kind, transactions=transactions)

        else:  # NON_REPEATABLE_READ
            s, p = gp["key"]
            f0 = base_by_key[(s, p)]
            reader, writer = _det_uuid(rng), _det_uuid(rng)
            fb = mk_fact(s, p, gp["new"], tc)
            write_facts.append(fb)
            # reader: two set-valued reads of the key (requires=[]), bracketing
            # the writer; the read key is the group's (subject, predicate).
            for k in (1, 2):
                # requires=[] is intentional: a concurrent read is set-valued —
                # its result depends on the backend's isolation level and is
                # scored by aml.eval.concurrency, so there is no single
                # recall_target. This is SAFE ONLY because validate_trace skips
                # txn_id-tagged turns: without that skip, requires=[] would trip
                # CONSISTENCY, since recall_targets.get(turn_id) is None (not an
                # empty set) for these held-out turns.
                gturns.append(Turn(
                    turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
                    content=f"{_question(f0)} (read {k})",
                    content_template=_question(f0),
                    timestamp=tc, requires=[], as_of=None, txn_id=reader))
            gturns.append(Turn(
                turn_id=_det_uuid(rng), role=TurnRole.USER,
                content=_statement(fb), content_template=_statement(fb),
                timestamp=tc, introduces=[fb.fact_id], txn_id=writer))
            transactions = [Transaction(reader), Transaction(writer)]  # concurrent
            group = ConcurrencyGroup(subject=s, predicate=p,
                                     anomaly=kind, transactions=transactions)

        groups.append(group)
        group_sessions.append(Session(
            session_id=_det_uuid(rng),
            start_time=gturns[0].timestamp, end_time=gturns[-1].timestamp,
            turns=gturns, tenant_id=None))

    # --- Oracle on the PREFIX ONLY (concurrent turns held out) -------------
    result = derive_ground_truth(base_facts, [prefix_session])

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W10,
        difficulty=difficulty,
        seed=seed,
        facts=list(result.final_facts) + write_facts,
        sessions=[prefix_session] + group_sessions,
        ground_truth=result.ground_truth,
        concurrency_groups=groups,
        schema_version=SCHEMA_VERSION_V2,  # W10 uses the v0.2 schema (§4.10)
    )


# ============================================================================
# Smoke check — run `python -m aml.generator.workloads.w10`
# ============================================================================

if __name__ == "__main__":
    from aml.generator.trace import trace_to_dict, validate_trace_schema
    from aml.generator.validators import validate_trace

    print("GRAFOMEM workloads/w10.py — Concurrency & Isolation generator\n")

    def counts(tr):
        by_anom = {}
        for g in tr.concurrency_groups:
            by_anom[g.anomaly.value] = by_anom.get(g.anomaly.value, 0) + 1
        tagged = sum(1 for s in tr.sessions for t in s.turns if t.txn_id is not None)
        return len(tr.facts), len(tr.concurrency_groups), by_anom, tagged

    # --- Test 1: easy structure -------------------------------------------
    tr = generate_w10(seed=0, difficulty=Difficulty.EASY)
    nf, ng, by_anom, tagged = counts(tr)
    assert ng == 3, f"expected 3 groups (1 of each), got {ng}"
    assert by_anom == {"lost_update": 1, "write_skew": 1, "non_repeatable_read": 1}, by_anom
    # facts: lost(1 base + 2 writes) + skew(2 base + 2 writes) + nrr(1 base + 1 write) = 9
    assert nf == 9, f"expected 9 facts, got {nf}"
    print(f"✓ Generates W10 easy                 "
          f"({nf} facts, {ng} groups {by_anom}, {tagged} txn-tagged turns)")

    # --- Test 2: validates clean (incl. W10 structural re-check) -----------
    issues = validate_trace(tr)
    assert issues == [], f"W10 easy flagged: {[str(i) for i in issues]}"
    print("✓ Validates clean                    "
          "(V1-V5 + REF/TENANT/V3 pass; W10 DAG/structure pass)")

    # --- Test 3: ground truth is PREFIX-only; groups well-formed -----------
    gt = tr.ground_truth
    # every recall_target turn is a prefix (txn_id=None) query
    tagged_ids = {t.turn_id for s in tr.sessions for t in s.turns if t.txn_id is not None}
    assert all(tid not in tagged_ids for tid in gt.recall_targets), \
        "no concurrent turn may carry a single-valued recall_target"
    # one prefix current-query probe per group, each targeting a base fact
    assert len(gt.recall_targets) == 3, f"expected 3 prefix probes, got {len(gt.recall_targets)}"
    # DAG: contending transactions are concurrent (no depends_on)
    for g in tr.concurrency_groups:
        assert len(g.transactions) == 2
        assert all(tx.depends_on == [] for tx in g.transactions), "writers must be concurrent"
    print("✓ GT prefix-only; groups well-formed "
          "(recall_targets only on prefix queries; 2 concurrent txns/group)")

    # --- Test 4: determinism (R1) -----------------------------------------
    a = trace_to_dict(generate_w10(seed=7, difficulty=Difficulty.EASY))
    b = trace_to_dict(generate_w10(seed=7, difficulty=Difficulty.EASY))
    a.pop("generated_at"); b.pop("generated_at")
    assert a == b, "same (seed, difficulty) produced different traces"
    c = trace_to_dict(generate_w10(seed=8, difficulty=Difficulty.EASY))
    c.pop("generated_at")
    assert a != c, "different seeds produced identical traces"
    print("✓ Deterministic across runs (R1)     (seed 7 identical; seed 8 differs)")

    # --- Test 5: JSON-Schema validation (v0.2 round-trip) ------------------
    d = trace_to_dict(tr)
    validate_trace_schema(d)
    assert "concurrency_groups" in d and len(d["concurrency_groups"]) == 3
    assert d["schema_version"].startswith("0.2"), d["schema_version"]
    print(f"✓ JSON-Schema validation passed      (schema {d['schema_version']}, "
          f"concurrency_groups serialised)")

    # --- Test 6: medium + hard generate and validate clean ----------------
    tm = generate_w10(seed=1, difficulty=Difficulty.MEDIUM)
    assert validate_trace(tm) == [], "W10 medium flagged"
    _, mg, mby, _ = counts(tm)
    th = generate_w10(seed=2, difficulty=Difficulty.HARD)
    assert validate_trace(th) == [], "W10 hard flagged"
    hf, hg, hby, _ = counts(th)
    validate_trace_schema(trace_to_dict(th))
    print(f"✓ Generates + validates medium+hard  "
          f"(medium {mg} groups {mby}; hard {hg} groups, {hf} facts)")

    print("\nAll W10 smoke checks green. Sequential prefix single-valued; "
          "concurrency groups planted + structurally validated. "
          "Eval-time outcome oracle (aml.eval.concurrency) scores them; "
          "extractor + runner are increment 6.")
