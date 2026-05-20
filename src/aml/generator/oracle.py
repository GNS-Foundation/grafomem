"""
GRAFOMEM ground-truth oracle — v0.1.1.

Derives the canonical GroundTruth for a trace from its raw event stream.
This is where the semantic rules from 01-workload-spec.md actually execute:

    - Rule O1 (§3.7) — intra-turn order: read -> introduce -> delete
    - Rule O2 (§3.8) — supersession chain repair under deletion
    - Rule O3 (§3.9) — cross-scope (tenant-global) deletion propagation
    - Finding 1     — active_memory gates on transaction time (introduction)
                      AND valid time (valid_from / effective valid_until)
    - Finding 2     — turns ordered by (timestamp, session_index, turn_index)

Input:  introduced_facts (ALL facts the generator created, incl. to-be-deleted)
        + sessions (turns referencing facts by fact_id).
Output: final_facts (surviving facts, chains repaired) + GroundTruth.

The generator assembles the returned final_facts + ground_truth into a Trace.

This module raises OracleError on inconsistencies it cannot resolve
(dangling references, V1/V2/V4 violations, supersession cycles) — fail-fast
for generator bugs, before they reach the eval harness.

Changelog
    v0.1.1 — explicit V1 guard in the forward pass (defense-in-depth: the
             oracle no longer assumes its turns came through the validating
             Turn constructor); tenant-partitioned active_memory loop for a
             T-times speedup on multi-tenant (W5) sweeps; guard-comment on
             the t_tx-vs-t_v split in the bi-temporal validity gate.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from aml.generator.trace import (
    Difficulty, Fact, GroundTruth, Session, Trace, Turn, TurnRole, Workload,
)


# A sentinel "all deletions applied" transaction time for final-state derivation.
_DISTANT_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


class OracleError(Exception):
    """Raised when the oracle detects an unresolvable inconsistency:
    dangling fact references, V2 (delete of non-existent/already-deleted),
    V4 (query requires an unavailable fact), or a supersession cycle."""


@dataclass(slots=True)
class OracleResult:
    """The oracle's output. The generator wraps these into a Trace."""
    final_facts: list[Fact]
    ground_truth: GroundTruth


@dataclass(slots=True)
class _TurnEvent:
    """A turn tagged with its position, for total ordering (Finding 2)."""
    turn: Turn
    session_index: int
    turn_index: int
    tenant_id: str | None


# ============================================================================
# Internal helpers
# ============================================================================

def _ordered_turn_events(sessions: list[Session]) -> list[_TurnEvent]:
    """Flatten and totally order all turns by (timestamp, session_index,
    turn_index). This is the transaction-time order (Finding 2). It stays
    consistent with fact `sequence` ordering by generator invariant: the
    generator assigns sequence monotonically as it walks turns.
    """
    events: list[_TurnEvent] = []
    for si, session in enumerate(sessions):
        for ti, turn in enumerate(session.turns):
            events.append(_TurnEvent(turn, si, ti, session.tenant_id))
    events.sort(key=lambda e: (e.turn.timestamp, e.session_index, e.turn_index))
    return events


def _effective_valid_until_at(
    fact: Fact,
    t_tx: datetime,
    fact_index: dict[bytes, Fact],
    deleted_facts: dict[bytes, datetime],
) -> datetime | None:
    """The valid_until of `fact` as the world stands at transaction time t_tx.

    Per Rule O2, a fact's valid_until is unchanged unless every successor in
    its supersession chain has been deleted by t_tx, in which case the fact
    is the surviving tail and its validity is open-ended (None).

    A middle-of-chain survivor keeps its original valid_until even when its
    immediate successor is deleted — preserving the temporal 'gap' left by a
    forgotten (deleted) fact. Only a surviving *tail* becomes open-ended.
    """
    succ_id = fact.superseded_by
    seen: set[bytes] = set()
    while succ_id is not None:
        if succ_id in seen:
            raise OracleError(
                f"supersession cycle detected near {succ_id.hex()}"
            )
        seen.add(succ_id)
        deleted_at = deleted_facts.get(succ_id)
        if deleted_at is None or deleted_at > t_tx:
            # A live successor exists -> fact keeps its original valid_until.
            return fact.valid_until
        # Successor deleted by t_tx -> walk to ITS successor.
        succ = fact_index.get(succ_id)
        succ_id = succ.superseded_by if succ is not None else None
    # No surviving successor -> fact is the current tail as of t_tx.
    return None


def _final_superseded_by(
    fact: Fact,
    fact_index: dict[bytes, Fact],
    deleted_set: set[bytes],
) -> bytes | None:
    """The repaired superseded_by pointer for the final state: the first
    surviving successor (skipping deleted ones), or None."""
    succ_id = fact.superseded_by
    seen: set[bytes] = set()
    while succ_id is not None and succ_id in deleted_set:
        if succ_id in seen:
            raise OracleError(
                f"supersession cycle detected near {succ_id.hex()}"
            )
        seen.add(succ_id)
        succ = fact_index.get(succ_id)
        succ_id = succ.superseded_by if succ is not None else None
    return succ_id


# ============================================================================
# Main derivation
# ============================================================================

def derive_ground_truth(
    introduced_facts: list[Fact],
    sessions: list[Session],
) -> OracleResult:
    """Derive final_facts + GroundTruth from a raw event stream.

    `introduced_facts` is the complete set of facts the generator created,
    including any that will be deleted. The oracle replays the turn stream
    in transaction order, tracks introduction and deletion times, repairs
    supersession chains, and computes per-query bi-temporal active_memory.
    """
    fact_index: dict[bytes, Fact] = {f.fact_id: f for f in introduced_facts}
    if len(fact_index) != len(introduced_facts):
        raise OracleError("duplicate fact_id in introduced_facts")

    events = _ordered_turn_events(sessions)

    # --- Forward pass: introduction tracking + deletion ledger (O1, O3) ---
    introduced_at: dict[bytes, datetime] = {}
    deleted_facts: dict[bytes, datetime] = {}

    for ev in events:
        turn = ev.turn
        t = turn.timestamp
        # V1 (defense-in-depth): intra-turn disjointness. Also enforced at
        # Turn construction, but the oracle does not trust that its turns
        # came through the validating constructor (e.g. tampered deserialized
        # input). Detonate before mutating any tracking set.
        v1_overlap = set(turn.introduces) & set(turn.deletes)
        if v1_overlap:
            raise OracleError(
                f"V1 violation: turn {turn.turn_id} introduces and deletes "
                f"the same fact(s) "
                f"{[b.hex()[:12] for b in sorted(v1_overlap)]}"
            )
        # O1 step 1 (read) is handled in the active_memory pass below.
        # O1 step 2: introduce.
        for fid in turn.introduces:
            if fid not in fact_index:
                raise OracleError(
                    f"turn {turn.turn_id} introduces unknown fact "
                    f"{fid.hex()}"
                )
            introduced_at.setdefault(fid, t)
        # O1 step 3: delete.
        for fid in turn.deletes:
            if fid not in fact_index:
                raise OracleError(
                    f"turn {turn.turn_id} deletes unknown fact {fid.hex()}"
                )
            # V2: target must already exist (introduced in a prior turn — V1
            # guarantees it wasn't introduced in this same turn).
            if fid not in introduced_at:
                raise OracleError(
                    f"V2 violation: turn {turn.turn_id} deletes fact "
                    f"{fid.hex()} that was never introduced"
                )
            if fid in deleted_facts:
                raise OracleError(
                    f"V2 violation: turn {turn.turn_id} deletes "
                    f"already-deleted fact {fid.hex()} "
                    f"(first deleted at {deleted_facts[fid].isoformat()})"
                )
            deleted_facts[fid] = t

    deleted_set = set(deleted_facts)

    # --- Build final-state facts with chain repair (O2) -------------------
    final_facts: list[Fact] = []
    for f in introduced_facts:
        if f.fact_id in deleted_set:
            continue  # deleted: excluded from final state (crypto shredding)
        final_sb = _final_superseded_by(f, fact_index, deleted_set)
        final_vu = _effective_valid_until_at(
            f, _DISTANT_FUTURE, fact_index, deleted_facts,
        )
        if final_sb == f.superseded_by and final_vu == f.valid_until:
            final_facts.append(f)  # unchanged
        else:
            final_facts.append(
                dataclasses.replace(
                    f, superseded_by=final_sb, valid_until=final_vu,
                )
            )

    # --- Per-query active_memory (Finding 1: bi-temporal) + recall_targets -
    recall_targets: dict[UUID, set[bytes]] = {}
    active_memory: dict[UUID, set[bytes]] = {}

    # Partition facts by tenant once. The active_memory loop then scans only
    # the current query's tenant slice, making the tenant filter structural
    # rather than a per-fact check. T-times speedup on multi-tenant (W5)
    # sweeps; a no-op (single None partition) for single-tenant workloads.
    facts_by_tenant: dict[str | None, list[Fact]] = {}
    for f in introduced_facts:
        facts_by_tenant.setdefault(f.tenant_id, []).append(f)

    for ev in events:
        turn = ev.turn
        if turn.role != TurnRole.AGENT_QUERY:
            continue
        t_tx = turn.timestamp                       # transaction time
        t_v = turn.as_of if turn.as_of is not None else t_tx  # valid time
        tid = ev.tenant_id

        active: set[bytes] = set()
        for f in facts_by_tenant.get(tid, ()):      # tenant slice only
            fid = f.fact_id
            # Transaction-time gate: the agent must have been told this fact.
            intro = introduced_at.get(fid)
            if intro is None or intro > t_tx:
                continue
            # Deletion gate (O3, tenant-global): excluded once deleted.
            dt = deleted_facts.get(fid)
            if dt is not None and dt <= t_tx:
                continue
            # Valid-time lower bound.
            if f.valid_from > t_v:
                continue
            # Valid-time upper bound, effective as of t_tx (O2-aware).
            #
            # NOTE: eff_vu is computed at TRANSACTION time t_tx (which
            # deletions have taken effect by query time) but compared against
            # VALID time t_v (when the fact was true in the world). This split
            # is deliberate and correct: deletions are transaction-time events,
            # validity is valid-time. Do NOT pass t_v into the helper — that
            # would treat valid-time-future deletions as already-applied, which
            # is a bug. (See annotation review, oracle v0.1.1.)
            eff_vu = _effective_valid_until_at(
                f, t_tx, fact_index, deleted_facts,
            )
            if eff_vu is not None and eff_vu <= t_v:
                continue
            active.add(fid)

        active_memory[turn.turn_id] = active

        req = set(turn.requires)
        recall_targets[turn.turn_id] = req
        # V4 + availability cross-check: every required fact must be
        # genuinely retrievable at this query. Catches generator bugs early.
        missing = req - active
        if missing:
            raise OracleError(
                f"V4/availability violation at query turn {turn.turn_id}: "
                f"required facts not retrievable "
                f"{[m.hex()[:12] for m in sorted(missing)]} "
                f"(not introduced by t_tx, deleted, wrong tenant, or "
                f"outside valid window)"
            )

    # --- Tenant partitions (W5) -------------------------------------------
    tenant_partitions: dict[str, set[bytes]] = {}
    for f in final_facts:
        if f.tenant_id is not None:
            tenant_partitions.setdefault(f.tenant_id, set()).add(f.fact_id)

    # --- Supersession chains from repaired (final) pointers ---------------
    points_to = {
        f.fact_id: f.superseded_by
        for f in final_facts if f.superseded_by is not None
    }
    pointed_to = set(points_to.values())
    superseded_chains: dict[bytes, list[bytes]] = {}
    for fid in points_to:
        if fid in pointed_to:
            continue  # not a chain head
        chain = [fid]
        seen = {fid}
        cur = fid
        while cur in points_to:
            cur = points_to[cur]
            if cur in seen:
                raise OracleError(
                    f"supersession cycle detected at {cur.hex()}"
                )
            seen.add(cur)
            chain.append(cur)
        superseded_chains[fid] = chain

    ground_truth = GroundTruth(
        recall_targets=recall_targets,
        active_memory=active_memory,
        superseded_chains=superseded_chains,
        tenant_partitions=tenant_partitions,
        deleted_facts=dict(deleted_facts),
    )
    return OracleResult(final_facts=final_facts, ground_truth=ground_truth)


# ============================================================================
# Smoke check — run `python oracle.py` for immediate diagnostic feedback
# ============================================================================

if __name__ == "__main__":
    from datetime import timedelta
    from uuid import uuid4

    print("GRAFOMEM oracle.py — ground-truth derivation v0.1.1\n")

    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def at(seconds: int) -> datetime:
        return base + timedelta(seconds=seconds)

    def mk_fact(pred, subj, obj, vf, seq, vu=None, sby=None, tenant=None):
        return Fact(
            predicate=pred, subject=subj, object=obj,
            valid_from=vf, sequence=seq, valid_until=vu,
            superseded_by=sby, tenant_id=tenant,
        )

    def user_turn(ts, introduces=(), deletes=()):
        return Turn(
            turn_id=uuid4(), role=TurnRole.USER,
            content="...", content_template="...", timestamp=ts,
            introduces=list(introduces), deletes=list(deletes),
        )

    def query_turn(ts, requires=(), as_of=None):
        return Turn(
            turn_id=uuid4(), role=TurnRole.AGENT_QUERY,
            content="?", content_template="?", timestamp=ts,
            requires=list(requires), as_of=as_of,
        )

    # --- Test 1: introduction-time gating (Finding 1) ---------------------
    # Both facts share valid_from = base, but are introduced at different
    # turns. A query between the two introductions must see ONLY the first.
    fa = mk_fact("lives_in", "u", "Rome", base, seq=1)
    fb = mk_fact("owns", "u", "bicycle", base, seq=2)
    s1 = Session(
        session_id=uuid4(), start_time=base, end_time=at(100),
        turns=[
            user_turn(at(10), introduces=[fa.fact_id]),
            query_turn(at(20), requires=[fa.fact_id]),          # only fa known
            user_turn(at(30), introduces=[fb.fact_id]),
            query_turn(at(40), requires=[fa.fact_id, fb.fact_id]),  # both known
        ],
    )
    r1 = derive_ground_truth([fa, fb], [s1])
    q_early = s1.turns[1].turn_id
    q_late = s1.turns[3].turn_id
    assert r1.ground_truth.active_memory[q_early] == {fa.fact_id}
    assert r1.ground_truth.active_memory[q_late] == {fa.fact_id, fb.fact_id}
    print("✓ Finding 1: introduction-time gating  "
          "(valid_from=base, but fb invisible until introduced)")

    # --- Test 2: bi-temporal supersession (W2) ----------------------------
    # Rome (valid 10-20) superseded by Milan (valid 20-). Both learned at t60.
    f_milan = mk_fact("lives_in", "u", "Milan", at(20), seq=4)
    f_rome = mk_fact("lives_in", "u", "Rome", at(10), seq=3,
                     vu=at(20), sby=f_milan.fact_id)
    s2 = Session(
        session_id=uuid4(), start_time=base, end_time=at(100),
        turns=[
            user_turn(at(60), introduces=[f_rome.fact_id, f_milan.fact_id]),
            query_turn(at(70), requires=[f_rome.fact_id], as_of=at(15)),   # pre
            query_turn(at(70), requires=[f_milan.fact_id], as_of=at(25)),  # post
            query_turn(at(70), requires=[f_milan.fact_id]),                # amb
        ],
    )
    r2 = derive_ground_truth([f_rome, f_milan], [s2])
    pre, post, amb = (s2.turns[i].turn_id for i in (1, 2, 3))
    assert r2.ground_truth.active_memory[pre] == {f_rome.fact_id}
    assert r2.ground_truth.active_memory[post] == {f_milan.fact_id}
    assert r2.ground_truth.active_memory[amb] == {f_milan.fact_id}
    print("✓ Finding/W2: bi-temporal supersession "
          "(as_of selects the right-as-of-time fact)")

    # --- Test 3: deletion timing + ledger (O1, O3) ------------------------
    # F introduced t10, deleted t50. G persists. Query at t30 sees F; t60 not.
    f_del = mk_fact("allergic_to", "u", "peanuts", base, seq=5)
    g_keep = mk_fact("speaks", "u", "Italian", base, seq=6)
    s3 = Session(
        session_id=uuid4(), start_time=base, end_time=at(100),
        turns=[
            user_turn(at(10), introduces=[f_del.fact_id, g_keep.fact_id]),
            query_turn(at(30), requires=[f_del.fact_id, g_keep.fact_id]),
            user_turn(at(50), deletes=[f_del.fact_id]),
            query_turn(at(60), requires=[g_keep.fact_id]),  # F now forbidden
        ],
    )
    r3 = derive_ground_truth([f_del, g_keep], [s3])
    q_before, q_after = s3.turns[1].turn_id, s3.turns[3].turn_id
    assert f_del.fact_id in r3.ground_truth.active_memory[q_before]
    assert f_del.fact_id not in r3.ground_truth.active_memory[q_after]
    assert g_keep.fact_id in r3.ground_truth.active_memory[q_after]
    assert r3.ground_truth.deleted_facts[f_del.fact_id] == at(50)
    assert all(f.fact_id != f_del.fact_id for f in r3.final_facts)
    print("✓ O1/O3: deletion timing + ledger      "
          "(active before t_del, gone after; excluded from final_facts)")

    # --- Test 4: chain repair on middle deletion (O2) ---------------------
    # Rome -> Milan -> Turin. Delete Milan (middle). Chain becomes Rome->Turin,
    # Rome.valid_until unchanged (gap preserved), Turin stays open tail.
    f_turin = mk_fact("lives_in", "v", "Turin", at(20), seq=9)
    f_milan2 = mk_fact("lives_in", "v", "Milan", at(10), seq=8,
                       vu=at(20), sby=f_turin.fact_id)
    f_rome2 = mk_fact("lives_in", "v", "Rome", base, seq=7,
                      vu=at(10), sby=f_milan2.fact_id)
    s4 = Session(
        session_id=uuid4(), start_time=base, end_time=at(100),
        turns=[
            user_turn(at(60), introduces=[
                f_rome2.fact_id, f_milan2.fact_id, f_turin.fact_id,
            ]),
            user_turn(at(70), deletes=[f_milan2.fact_id]),
        ],
    )
    r4 = derive_ground_truth([f_rome2, f_milan2, f_turin], [s4])
    chains = r4.ground_truth.superseded_chains
    assert chains == {f_rome2.fact_id: [f_rome2.fact_id, f_turin.fact_id]}, \
        f"unexpected chains: {chains}"
    final_by_id = {f.fact_id: f for f in r4.final_facts}
    assert f_milan2.fact_id not in final_by_id          # deleted
    assert final_by_id[f_rome2.fact_id].superseded_by == f_turin.fact_id  # rewired
    assert final_by_id[f_rome2.fact_id].valid_until == at(10)  # gap preserved
    assert final_by_id[f_turin.fact_id].valid_until is None    # surviving tail
    print("✓ O2: chain repair (middle delete)     "
          "(Rome->Turin rewired, gap preserved, tail open)")

    # --- Test 5: OracleError on dangling reference ------------------------
    phantom = b"\xab" * 16
    s5 = Session(
        session_id=uuid4(), start_time=base, end_time=at(10),
        turns=[user_turn(at(5), introduces=[phantom])],
    )
    try:
        derive_ground_truth([fa], [s5])
        raise AssertionError("expected OracleError on dangling fact ref")
    except OracleError as e:
        assert "unknown fact" in str(e)
    print("✓ Fail-fast: OracleError on dangling   "
          "(turn introduces a fact not in the fact set)")

    # --- Test 6: explicit V1 guard (v0.1.1) -------------------------------
    # Build a V1-violating turn by mutating a constructed turn's `deletes`
    # to overlap `introduces` — bypassing Turn.__post_init__, simulating a
    # tampered/deserialized turn the oracle must not trust.
    fz = mk_fact("knows", "u", "Aria", base, seq=7)
    bad_turn = user_turn(at(5), introduces=[fz.fact_id])
    object.__setattr__(bad_turn, "deletes", [fz.fact_id])  # now intro ∩ del != ∅
    s6 = Session(
        session_id=uuid4(), start_time=base, end_time=at(10),
        turns=[bad_turn],
    )
    try:
        derive_ground_truth([fz], [s6])
        raise AssertionError("expected OracleError on V1 violation")
    except OracleError as e:
        assert "V1 violation" in str(e)
    print("✓ Defense-in-depth: V1 guard           "
          "(oracle detonates on introduce ∩ delete, not trusting Turn)")

    print("\nAll oracle smoke checks green. Ready for validators.py + W1.")
