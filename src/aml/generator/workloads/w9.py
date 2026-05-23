"""
GRAFOMEM W9 — Cross-Session Deletion generator ("Right to Be Forgotten").

W9 is the cross-session extension of W6. W6 proves single-store deletion (forget
HERE, gone HERE); W9 proves that "forget" is GLOBAL across the sessions of one
backend instance: delete a fact in session B, probe for it from session C, and
it must be gone there too. This is the home of the reserved capability
CROSS_SESSION_PROPAGATION (02 §3; spec §4.9).

Construction (the only delta from W6 is WHERE turns sit):
  For each subject j, its three turns land in three DISTINCT sessions:
      introduce -> session  j      % n_sessions
      delete    -> session (j + 1) % n_sessions
      probe     -> session (j + 2) % n_sessions
  With n_sessions >= 3 these are pairwise distinct, and crucially the
  deleted-PROBE session != the DELETE session. That single inequality is what
  makes propagation testable: a backend that scopes deletion to the session that
  issued it will filter the fact in the delete session but LEAK it in the probe
  session. If probe and delete shared a session (as they may in W6), even a
  session-local backend would look clean and the test would be blind.

Probes, exactly as in W6 (two directions, by construction):
  - DELETED probe (requires=[], content = the deleted fact's question): correct
    answer is empty; the deleted fact must NOT resurface in another session.
    Failure here = CROSS-SESSION LEAKAGE (a propagation violation).
  - SURVIVOR probe (requires=[F_surv]): a non-deleted fact, asked from a session
    other than the one it was written in. It must still be retrievable.
    Failure here = OVER-DELETION (or a backend whose reads don't span sessions).

Timestamps stay monotonic in creation order (all intros < all deletes < all
probes), exactly as W6, so the canonical (timestamp, session_index, turn_index)
order is intros -> deletes -> probes regardless of the session each turn carries.
Session placement is therefore invisible to the oracle's transaction order
(V2 "delete a live target" holds) and to validators; it matters only to a runner
that dispatches each turn to its session's view of the backend (see run_w9).

Contract notes (verified against oracle/harness/validators/interface):
  - delete(ref) carries NO session/option context, so cross-session semantics
    cannot be exercised through the shared single-instance harness; run_w9 owns
    a per-session dispatch replay. interface.py / harness.py are untouched.
  - The oracle RAISES if a query requires a deleted fact, so deleted probes
    require nothing (recall_target = {}); V4 is satisfied.
  - gt.deleted_facts records {fid: deleted_at}; cross-session leakage is scored
    in run_w9 against it, exactly as W6 scores single-store leakage.

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
    Session,
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
    _statement,
)

_PRED_NAMES = list(_PREDICATES)


@dataclass(frozen=True)
class _W9Params:
    n_subjects: int          # <= len(_PERSONS) = 44
    facts_per_subject: int   # >= 2 so every deleted subject keeps a survivor
    n_sessions: int          # >= 3 so intro/delete/probe are pairwise distinct
    # one fact per subject is deleted; the rest survive.


_W9_PARAMS = {
    Difficulty.EASY:   _W9Params(n_subjects=10, facts_per_subject=2, n_sessions=3),
    Difficulty.MEDIUM: _W9Params(n_subjects=25, facts_per_subject=3, n_sessions=5),
    Difficulty.HARD:   _W9Params(n_subjects=44, facts_per_subject=4, n_sessions=10),
}


def _obj_for(rng, predicate: str, subject: str):
    pool_name, _, _ = _PREDICATES[predicate]
    pool = _POOLS[pool_name]
    obj = rng.choice(pool)
    if pool_name == "PERSONS":
        while obj == subject:
            obj = rng.choice(pool)
    return obj


def generate_w9(seed: int, difficulty: Difficulty) -> Trace:
    p = _W9_PARAMS[difficulty]
    if p.n_sessions < 3:
        raise ValueError("W9 requires n_sessions >= 3 (intro/delete/probe distinct)")
    rng = _make_rng(Workload.W9, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    n = p.n_sessions

    subjects = list(_PERSONS)
    rng.shuffle(subjects)
    subjects = subjects[: p.n_subjects]

    # Per subject: facts_per_subject distinct-predicate facts; the first is the
    # one we will delete, the rest survive.
    seq = 0
    per_subject: list[tuple[int, Fact, list[Fact]]] = []   # (j, deleted, survivors)
    all_facts: list[Fact] = []
    for j, subj in enumerate(subjects):
        preds = rng.sample(_PRED_NAMES, p.facts_per_subject)
        sfacts = []
        for pr in preds:
            seq += 1
            sfacts.append(Fact(predicate=pr, subject=subj,
                               object=_obj_for(rng, pr, subj),
                               valid_from=t0, sequence=seq, importance=1.0))
        all_facts.extend(sfacts)
        per_subject.append((j, sfacts[0], sfacts[1:]))      # delete first, keep rest

    def intro_sess(j):  return j % n
    def del_sess(j):    return (j + 1) % n
    def probe_sess(j):  return (j + 2) % n

    # --- turns: introduce all -> delete one per subject -> probe -----------
    # Timestamps are monotonic in creation order (intros, then deletes, then
    # probes); the session index each turn carries is what run_w9 dispatches on.
    tagged: list[tuple[int, Turn]] = []                      # (session_index, turn)
    k = 0

    def at(i):
        return t0 + timedelta(seconds=i)

    for j, deleted, survivors in per_subject:               # introduce everything
        si = intro_sess(j)
        for f in [deleted, *survivors]:
            tagged.append((si, Turn(turn_id=_det_uuid(rng), role=TurnRole.USER,
                          content=_statement(f), content_template=_statement(f),
                          timestamp=at(k), introduces=[f.fact_id])))
            k += 1

    for j, deleted, _ in per_subject:                        # delete one per subject
        si = del_sess(j)
        tagged.append((si, Turn(turn_id=_det_uuid(rng), role=TurnRole.USER,
                      content=f"(forget) {_statement(deleted)}",
                      content_template="(forget) {stmt}",
                      timestamp=at(k), deletes=[deleted.fact_id])))
        k += 1

    # probes (after all deletions): one deleted-probe + one survivor-probe per
    # subject, both issued from probe_sess(j) (!= del_sess(j)).
    probe_specs = []
    for j, deleted, survivors in per_subject:
        ps = probe_sess(j)
        probe_specs.append((ps, "deleted", deleted, None))
        if survivors:
            probe_specs.append((ps, "survivor", deleted, survivors[0]))
    rng.shuffle(probe_specs)
    for ps, kind, deleted, surv in probe_specs:
        if kind == "deleted":
            tagged.append((ps, Turn(turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
                          content=_question(deleted),
                          content_template=_question(deleted),
                          timestamp=at(k), requires=[], as_of=None)))
        else:
            tagged.append((ps, Turn(turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
                          content=_question(surv),
                          content_template=_question(surv),
                          timestamp=at(k), requires=[surv.fact_id], as_of=None)))
        k += 1

    # --- group tagged turns into sessions ----------------------------------
    by_sess: dict[int, list[Turn]] = {s: [] for s in range(n)}
    for si, turn in tagged:
        by_sess[si].append(turn)
    sessions: list[Session] = []
    for s in range(n):
        block = sorted(by_sess[s], key=lambda t: t.timestamp)
        if not block:
            continue
        sessions.append(Session(
            session_id=_det_uuid(rng),
            start_time=block[0].timestamp,
            end_time=block[-1].timestamp,
            turns=block,
            tenant_id=None,             # W9 is single-tenant; sessions != tenants
        ))

    result = derive_ground_truth(all_facts, sessions)

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W9,
        difficulty=difficulty,
        seed=seed,
        # Like W6, W9 carries the FULL introduced tape (survivors + deleted): the
        # runner must replay every write before the deletes fire. The surviving
        # set is the tape minus ground_truth.deleted_facts.
        facts=all_facts,
        sessions=sessions,
        ground_truth=result.ground_truth,
    )


# ============================================================================
# Smoke check — run `python -m aml.generator.workloads.w9`
# ============================================================================

if __name__ == "__main__":
    import hashlib
    import json

    from aml.generator.trace import trace_to_dict
    from aml.generator.validators import validate_trace

    print("GRAFOMEM workloads/w9.py — Cross-Session Deletion generator\n")

    def parts(tr):
        q = [t for s in tr.sessions for t in s.turns if t.role == TurnRole.AGENT_QUERY]
        deleted_probes = [t for t in q if not t.requires]
        survivor_probes = [t for t in q if t.requires]
        return q, deleted_probes, survivor_probes

    # --- Test 1: structure across difficulties ----------------------------
    for diff, params in _W9_PARAMS.items():
        tr = generate_w9(seed=0, difficulty=diff)
        q, dp, sp = parts(tr)
        n_deleted = len(tr.ground_truth.deleted_facts)
        survivors = len(tr.facts) - n_deleted
        assert n_deleted == params.n_subjects, f"{diff.value}: {n_deleted} deleted"
        assert len(dp) == params.n_subjects, f"{diff.value}: deleted-probes {len(dp)}"
        assert len(tr.sessions) == params.n_sessions, f"{diff.value}: sessions"
        print(f"  {diff.value:7s}: {len(tr.facts):3d} tape ({survivors:3d} survive, "
              f"{n_deleted:2d} deleted), {len(sp):2d} survivor + {len(dp):2d} deleted probes, "
              f"{len(tr.sessions):2d} sessions")

    # --- Test 2: recall_targets shape (survivor singleton, deleted empty) --
    tr = generate_w9(seed=0, difficulty=Difficulty.MEDIUM)
    rt = tr.ground_truth.recall_targets
    _, dp, sp = parts(tr)
    assert all(not rt[t.turn_id] for t in dp), "deleted-probe target must be empty"
    assert all(len(rt[t.turn_id]) == 1 for t in sp), "survivor-probe target must be one fact"
    print("\n✓ recall_targets        (deleted-probes empty, survivor-probes singleton)")

    # --- Test 3: THE W9 invariant — delete session != deleted-probe session -
    # Reconstruct, from the finished trace, which session each delete and each
    # deleted-probe lives in, keyed by subject (via the deleted fact's content).
    fid_subject = {f.fact_id: f.subject for f in tr.facts}
    del_session_of_subject = {}
    probe_session_of_subject = {}
    # map a deleted fact's question text -> subject (subjects are distinct)
    q_to_subject = {}
    for fid in tr.ground_truth.deleted_facts:
        f = next(f for f in tr.facts if f.fact_id == fid)
        q_to_subject[_question(f)] = f.subject
    for si, s in enumerate(tr.sessions):
        for t in s.turns:
            for fid in t.deletes:
                del_session_of_subject[fid_subject[fid]] = si
            if t.role == TurnRole.AGENT_QUERY and not t.requires:
                subj = q_to_subject.get(t.content)
                if subj is not None:
                    probe_session_of_subject[subj] = si
    common = set(del_session_of_subject) & set(probe_session_of_subject)
    assert common, "no subjects matched between deletes and deleted-probes"
    assert all(del_session_of_subject[s] != probe_session_of_subject[s] for s in common), \
        "deleted-probe MUST be issued from a different session than the delete"
    print(f"✓ Cross-session split   (all {len(common)} subjects: delete-session "
          f"!= deleted-probe-session — propagation is testable)")

    # --- Test 4: over-deletion testable — each deleted subject keeps a surv -
    deleted = tr.ground_truth.deleted_facts
    by_subject_survivors = {}
    for f in tr.facts:
        if f.fact_id in deleted:
            continue
        by_subject_survivors[f.subject] = by_subject_survivors.get(f.subject, 0) + 1
    deleted_subjects = {f.subject for f in tr.facts if f.fact_id in deleted}
    assert all(by_subject_survivors.get(s, 0) >= 1 for s in deleted_subjects)
    print(f"✓ Over-deletion testable ({len(deleted_subjects)} deleted subjects each "
          f"retain a survivor)")

    # --- Test 5: validators clean (interleaved sessions are legal) ---------
    for diff in _W9_PARAMS:
        v = validate_trace(generate_w9(seed=1, difficulty=diff))
        assert not v, f"{diff.value}: {len(v)} violation(s): {v[:3]}"
    print("✓ Validators clean      (V1-V5 pass on interleaved sessions)")

    # --- Test 6: determinism ----------------------------------------------
    def chash(tr):
        d = trace_to_dict(tr); d.pop("trace_id", None); d.pop("generated_at", None)
        return hashlib.blake2b(json.dumps(d, sort_keys=True,
                               separators=(",", ":")).encode(), digest_size=16).hexdigest()
    assert chash(generate_w9(seed=2, difficulty=Difficulty.HARD)) == \
           chash(generate_w9(seed=2, difficulty=Difficulty.HARD))
    print("✓ Deterministic (R1)    (hard seed 2 reproduces)")

    print("\nAll W9 smoke checks green. Next: cross-session backends "
          "(propagating / session_local / no_propagation) + run_w9.")
