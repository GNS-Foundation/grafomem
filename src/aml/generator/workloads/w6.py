"""
GRAFOMEM W6 — Deletion / Leakage generator.

W6 tests the privacy primitive a memory protocol must guarantee: when the agent
is told to forget a fact, is it actually gone? Each subject is given several
facts; one per subject is then DELETED (a delete turn), the rest survive. After
the deletions, two kinds of probe are issued:

  - SURVIVOR probe (requires=[F_surv]): asks about a fact that was NOT deleted.
    It must still be retrievable. Failure here = OVER-DELETION (a backend that
    drops more than it was asked to — e.g. purging a whole subject).

  - DELETED probe (requires=[], content = the deleted fact's question): asks
    about a fact that WAS deleted. Its correct answer is empty; the deleted fact
    must NOT appear. Failure here = LEAKAGE (the forgotten fact resurfaces — a
    privacy violation).

Two directions, by construction: a backend can fail by remembering too much
(leakage) or forgetting too much (over-deletion); only exact-fact deletion
avoids both. Because every deleted fact shares a subject with a survivor,
subject-level (over-broad) deletion is detectable as over-deletion.

Contract notes (verified against oracle/harness/validators):
  - The oracle RAISES if a query requires a deleted fact, so deleted probes
    require nothing (recall_target = {}); V4 is satisfied (nothing required).
  - gt.deleted_facts records {fid: deleted_at}; leakage is scored in run_w6
    against it (retrieved intersect deleted-before-query), not in the trace
    validators.
  - The harness dispatches delete(ref) only to HARD_DELETE backends; others
    no-op, so the content persists and leaks (the leaky baseline).

Reuses W1 vocab/templates and the W1 oracle + validators unchanged.
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

_PRED_NAMES = list(_PREDICATES)


@dataclass(frozen=True)
class _W6Params:
    n_subjects: int          # <= len(_PERSONS) = 44
    facts_per_subject: int   # distinct predicates per subject
    n_sessions: int
    # one fact per subject is deleted; the rest survive.


_W6_PARAMS = {
    Difficulty.EASY:   _W6Params(n_subjects=10, facts_per_subject=2, n_sessions=1),
    Difficulty.MEDIUM: _W6Params(n_subjects=25, facts_per_subject=3, n_sessions=4),
    Difficulty.HARD:   _W6Params(n_subjects=44, facts_per_subject=4, n_sessions=10),
}


def _obj_for(rng, predicate: str, subject: str):
    pool_name, _, _ = _PREDICATES[predicate]
    pool = _POOLS[pool_name]
    obj = rng.choice(pool)
    if pool_name == "PERSONS":
        while obj == subject:
            obj = rng.choice(pool)
    return obj


def generate_w6(seed: int, difficulty: Difficulty) -> Trace:
    p = _W6_PARAMS[difficulty]
    rng = _make_rng(Workload.W6, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    subjects = list(_PERSONS)
    rng.shuffle(subjects)
    subjects = subjects[: p.n_subjects]

    # Per subject: facts_per_subject distinct-predicate facts; the first is the
    # one we will delete, the rest survive.
    seq = 0
    per_subject: list[tuple[Fact, list[Fact]]] = []   # (deleted, survivors)
    all_facts: list[Fact] = []
    for subj in subjects:
        preds = rng.sample(_PRED_NAMES, p.facts_per_subject)
        sfacts = []
        for pr in preds:
            seq += 1
            sfacts.append(Fact(predicate=pr, subject=subj,
                               object=_obj_for(rng, pr, subj),
                               valid_from=t0, sequence=seq, importance=1.0))
        all_facts.extend(sfacts)
        per_subject.append((sfacts[0], sfacts[1:]))     # delete first, keep rest

    # --- turns: introduce all -> delete one per subject -> probe -----------
    turns: list[Turn] = []
    k = 0

    def at(i):
        return t0 + timedelta(seconds=i)

    for f in all_facts:                                  # introduce everything
        turns.append(Turn(turn_id=_det_uuid(rng), role=TurnRole.USER,
                          content=_statement(f), content_template=_statement(f),
                          timestamp=at(k), introduces=[f.fact_id]))
        k += 1
    for deleted, _ in per_subject:                       # delete one per subject
        turns.append(Turn(turn_id=_det_uuid(rng), role=TurnRole.USER,
                          content=f"(forget) {_statement(deleted)}",
                          content_template="(forget) {stmt}",
                          timestamp=at(k), deletes=[deleted.fact_id]))
        k += 1

    # probes (after all deletions): one deleted-probe + one survivor-probe per
    # subject. Deleted probe requires nothing (its answer is empty).
    probe_specs = []
    for deleted, survivors in per_subject:
        probe_specs.append(("deleted", deleted, None))
        if survivors:
            probe_specs.append(("survivor", deleted, survivors[0]))
    rng.shuffle(probe_specs)
    for kind, deleted, surv in probe_specs:
        if kind == "deleted":
            turns.append(Turn(turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
                              content=_question(deleted),
                              content_template=_question(deleted),
                              timestamp=at(k), requires=[], as_of=None))
        else:
            turns.append(Turn(turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
                              content=_question(surv),
                              content_template=_question(surv),
                              timestamp=at(k), requires=[surv.fact_id], as_of=None))
        k += 1

    sessions = _split_into_sessions(rng, turns, p.n_sessions)
    result = derive_ground_truth(all_facts, sessions)

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W6,
        difficulty=difficulty,
        seed=seed,
        # W6 carries the FULL introduced tape (survivors + to-be-deleted), not
        # just survivors. Deletion is the one operation that *removes* facts, and
        # the harness must replay every write — with subject/predicate metadata —
        # before the delete turns fire, or delete-by-subject backends never see
        # the deleted fact's subject. The surviving set is the tape minus
        # ground_truth.deleted_facts. (For non-deletion workloads nothing is
        # removed, so trace.facts == survivors.)
        facts=all_facts,
        sessions=sessions,
        ground_truth=result.ground_truth,
    )


# ============================================================================
# Smoke check — run `python -m aml.generator.workloads.w6`
# ============================================================================

if __name__ == "__main__":
    import hashlib
    import json

    from aml.generator.trace import trace_to_dict
    from aml.generator.validators import validate_trace

    print("GRAFOMEM workloads/w6.py — Deletion / Leakage generator\n")

    def parts(tr):
        q = [t for s in tr.sessions for t in s.turns if t.role == TurnRole.AGENT_QUERY]
        deleted_probes = [t for t in q if not t.requires]
        survivor_probes = [t for t in q if t.requires]
        n_del = [t for s in tr.sessions for t in s.turns if t.deletes]
        return q, deleted_probes, survivor_probes, n_del

    # --- Test 1: structure across difficulties ----------------------------
    for diff, params in _W6_PARAMS.items():
        tr = generate_w6(seed=0, difficulty=diff)
        q, dp, sp, dels = parts(tr)
        n_deleted = len(tr.ground_truth.deleted_facts)
        survivors = len(tr.facts) - n_deleted
        assert n_deleted == params.n_subjects, f"{diff.value}: {n_deleted} deleted != {params.n_subjects}"
        assert len(dp) == params.n_subjects, f"{diff.value}: deleted-probes {len(dp)}"
        print(f"  {diff.value:7s}: {len(tr.facts):3d} tape ({survivors:3d} survive, "
              f"{n_deleted:2d} deleted), {len(sp):2d} survivor-probes + {len(dp):2d} deleted-probes")

    # --- Test 2: recall_targets shape (survivor non-empty, deleted empty) --
    tr = generate_w6(seed=0, difficulty=Difficulty.MEDIUM)
    rt = tr.ground_truth.recall_targets
    _, dp, sp, _ = parts(tr)
    assert all(not rt[t.turn_id] for t in dp), "deleted-probe target must be empty"
    assert all(len(rt[t.turn_id]) == 1 for t in sp), "survivor-probe target must be one fact"
    print(f"\n✓ recall_targets        (deleted-probes empty, survivor-probes singleton)")

    # --- Test 3: leakage is testable — deleted facts are in the tape & dated -
    deleted = tr.ground_truth.deleted_facts
    assert all(isinstance(ts, datetime) for ts in deleted.values())
    tape_ids = {f.fact_id for f in tr.facts}
    # deleted facts ARE in the tape (the harness must write them so they can be
    # deleted / can leak), and every one is recorded in the deletion ledger:
    assert set(deleted) <= tape_ids, "deleted facts must be present in the tape"
    print(f"✓ Deletion ledger       ({len(deleted)} facts deleted w/ timestamps, "
          f"present in tape so writable & leak-testable)")

    # --- Test 4: over-deletion is testable — each deleted subject keeps a surv -
    by_subject_survivors = {}
    for f in tr.facts:
        if f.fact_id in deleted:
            continue
        by_subject_survivors.setdefault(f.subject, 0)
        by_subject_survivors[f.subject] += 1
    deleted_subjects = {f.subject for f in tr.facts if f.fact_id in deleted}
    # every subject that had a deletion still has >=1 SURVIVING fact in the tape,
    # so a delete-by-subject backend can be caught over-deleting it:
    assert all(by_subject_survivors.get(s, 0) >= 1 for s in deleted_subjects)
    print(f"✓ Over-deletion testable ({len(deleted_subjects)} deleted subjects each "
          f"retain a survivor sharing the subject)")

    # --- Test 5: validators clean -----------------------------------------
    for diff in _W6_PARAMS:
        v = validate_trace(generate_w6(seed=1, difficulty=diff))
        assert not v, f"{diff.value}: {len(v)} violation(s): {v[:3]}"
    print("✓ Validators clean      (V1/V2/V4 pass; deletes well-formed)")

    # --- Test 6: determinism ----------------------------------------------
    def chash(tr):
        d = trace_to_dict(tr); d.pop("trace_id", None); d.pop("generated_at", None)
        return hashlib.blake2b(json.dumps(d, sort_keys=True,
                               separators=(",", ":")).encode(), digest_size=16).hexdigest()
    assert chash(generate_w6(seed=2, difficulty=Difficulty.HARD)) == \
           chash(generate_w6(seed=2, difficulty=Difficulty.HARD))
    print("✓ Deterministic (R1)    (hard seed 2 reproduces)")

    print("\nAll W6 smoke checks green. Next: delete backends (soft/honest/coarse) "
          "+ run_w6 (leakage + over-deletion).")
