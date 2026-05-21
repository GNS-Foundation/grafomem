"""
GRAFOMEM W5 — Tenant Isolation generator.

W5 is the second half of the privacy axis (W6 is the first). The boundary is the
TENANT rather than the deletion: a fact owned by tenant A must never surface for
a query issued by tenant B, even when the two are semantically indistinguishable.

Construction. N tenants share the SAME subjects and predicates, but each tenant
holds a DIFFERENT object for a given (subject, predicate). So every tenant has an
"Alice / lives_in" fact, but tenant-a's is Rome, tenant-b's is Paris, etc. A
query in tenant-a ("Where does Alice live?") is therefore byte-identical to the
query a different tenant would ask, and a store that does not scope retrieval by
tenant will surface the other tenants' Alice-facts — a cross-tenant leak.

Each tenant gets its own session (Session.tenant_id). All introductions precede
all queries so that, at query time, every other tenant's fact is already in the
store and a leak is actually possible (mirrors W6: intros -> probes).

Probes are in-tenant: requires=[own-tenant fact], so recall_target is that one
fact (the oracle's tenant-partitioned active_memory scopes the universe to the
query's tenant). Cross-tenant LEAKAGE is not a recall target — it is scored in
run_w5 as any retrieved fact whose tenant differs from the query's (M7).

Contract notes (verified):
  - The oracle scans only the query tenant's fact slice and RAISES if a query
    requires a fact outside its tenant, so in-tenant probes require only own
    facts (no cross-tenant requires).
  - fact_id = hash(predicate, subject, object, valid_from) and does NOT include
    tenant_id; distinct objects per tenant keep fact_ids unique.
  - The harness dispatches tenant_id only to MULTI_TENANT backends (added with
    the W5 backends); a naive store sees tenant_id=None, pools all tenants, and
    leaks.

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
class _W5Params:
    n_tenants: int
    n_subjects: int          # shared across all tenants; <= len(_PERSONS) = 44
    # one fact per (tenant, subject); one in-tenant probe per (tenant, subject).


_W5_PARAMS = {
    Difficulty.EASY:   _W5Params(n_tenants=2, n_subjects=6),
    Difficulty.MEDIUM: _W5Params(n_tenants=3, n_subjects=14),
    Difficulty.HARD:   _W5Params(n_tenants=4, n_subjects=30),
}


def _tenant_id(i: int) -> str:
    return f"tenant-{chr(97 + i)}"        # tenant-a, tenant-b, ...


def _distinct_objects(rng, predicate: str, subject: str, n: int) -> list[str]:
    """n distinct objects for (subject, predicate) — one per tenant, so each
    tenant's fact is a distinct fact_id (which ignores tenant_id)."""
    pool_name, _, _ = _PREDICATES[predicate]
    pool = [o for o in _POOLS[pool_name] if not (pool_name == "PERSONS" and o == subject)]
    rng.shuffle(pool)
    if len(pool) < n:
        # widen by suffixing — keeps determinism and uniqueness
        base = list(pool)
        pool = base + [f"{o} ({k})" for k in range(2, n) for o in base]
    return pool[:n]


def generate_w5(seed: int, difficulty: Difficulty) -> Trace:
    p = _W5_PARAMS[difficulty]
    rng = _make_rng(Workload.W5, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    subjects = list(_PERSONS)
    rng.shuffle(subjects)
    subjects = subjects[: p.n_subjects]

    # Each subject gets ONE predicate, shared across all tenants, so the queries
    # are identical across tenants (maximal cross-tenant temptation).
    preds = {s: rng.choice(_PRED_NAMES) for s in subjects}

    # facts[tenant_idx][subject] = Fact (distinct object per tenant)
    seq = 0
    facts_by_tenant: list[dict[str, Fact]] = []
    all_facts: list[Fact] = []
    objects = {s: _distinct_objects(rng, preds[s], s, p.n_tenants) for s in subjects}
    for ti in range(p.n_tenants):
        tid = _tenant_id(ti)
        tfacts: dict[str, Fact] = {}
        for s in subjects:
            seq += 1
            f = Fact(predicate=preds[s], subject=s, object=objects[s][ti],
                     valid_from=t0, sequence=seq, importance=1.0, tenant_id=tid)
            tfacts[s] = f
            all_facts.append(f)
        facts_by_tenant.append(tfacts)

    # Timeline: ALL intros (every tenant) precede ALL queries, so at query time
    # every other tenant's fact is in the store and a leak is possible.
    n_facts = len(all_facts)

    def at(i):
        return t0 + timedelta(seconds=i)

    intro_turns: list[list[Turn]] = [[] for _ in range(p.n_tenants)]
    probe_turns: list[list[Turn]] = [[] for _ in range(p.n_tenants)]

    k = 0                                                    # all intros first
    for ti in range(p.n_tenants):
        for s in subjects:
            f = facts_by_tenant[ti][s]
            intro_turns[ti].append(Turn(
                turn_id=_det_uuid(rng), role=TurnRole.USER,
                content=_statement(f), content_template=_statement(f),
                timestamp=at(k), introduces=[f.fact_id]))
            k += 1

    k = n_facts                                              # then all probes
    for ti in range(p.n_tenants):
        for s in subjects:
            f = facts_by_tenant[ti][s]
            probe_turns[ti].append(Turn(
                turn_id=_det_uuid(rng), role=TurnRole.AGENT_QUERY,
                content=_question(f), content_template=_question(f),
                timestamp=at(k), requires=[f.fact_id], as_of=None))
            k += 1

    sessions: list[Session] = []
    for ti in range(p.n_tenants):
        turns = intro_turns[ti] + probe_turns[ti]            # intros early, probes late
        sessions.append(Session(
            session_id=_det_uuid(rng),
            start_time=turns[0].timestamp,
            end_time=turns[-1].timestamp,
            turns=turns,
            tenant_id=_tenant_id(ti)))

    result = derive_ground_truth(all_facts, sessions)

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W5,
        difficulty=difficulty,
        seed=seed,
        facts=all_facts,
        sessions=sessions,
        ground_truth=result.ground_truth,
    )


# ============================================================================
# Smoke check — run `python -m aml.generator.workloads.w5`
# ============================================================================

if __name__ == "__main__":
    import hashlib
    import json

    from aml.generator.trace import trace_to_dict
    from aml.generator.validators import validate_trace

    print("GRAFOMEM workloads/w5.py — Tenant Isolation generator\n")

    def parts(tr):
        q = [t for s in tr.sessions for t in s.turns if t.role == TurnRole.AGENT_QUERY]
        tenants = {s.tenant_id for s in tr.sessions}
        return q, tenants

    # --- Test 1: structure across difficulties ----------------------------
    for diff, params in _W5_PARAMS.items():
        tr = generate_w5(seed=0, difficulty=diff)
        q, tenants = parts(tr)
        assert len(tenants) == params.n_tenants, f"{diff.value}: {len(tenants)} tenants"
        assert len(tr.facts) == params.n_tenants * params.n_subjects
        assert len(q) == params.n_tenants * params.n_subjects
        print(f"  {diff.value:7s}: {params.n_tenants} tenants x {params.n_subjects} subjects "
              f"= {len(tr.facts):3d} facts, {len(q):3d} in-tenant probes")

    # --- Test 2: recall_targets — each probe targets exactly its own fact --
    tr = generate_w5(seed=0, difficulty=Difficulty.MEDIUM)
    rt = tr.ground_truth.recall_targets
    q, _ = parts(tr)
    assert all(len(rt[t.turn_id]) == 1 for t in q), "each in-tenant probe targets one fact"
    # the targeted fact's tenant matches the querying session's tenant:
    fid_tenant = {f.fact_id: f.tenant_id for f in tr.facts}
    for s in tr.sessions:
        for t in s.turns:
            if t.role == TurnRole.AGENT_QUERY:
                (tgt,) = rt[t.turn_id]
                assert fid_tenant[tgt] == s.tenant_id, "target must be own-tenant fact"
    print("\n✓ recall_targets        (each probe -> its own tenant's single fact)")

    # --- Test 3: leak is testable — same (subject,predicate) in >1 tenant --
    by_sp: dict[tuple, set[str]] = {}
    for f in tr.facts:
        by_sp.setdefault((f.subject, f.predicate), set()).add(f.tenant_id)
    shared = [sp for sp, tids in by_sp.items() if len(tids) >= 2]
    assert len(shared) == _W5_PARAMS[Difficulty.MEDIUM].n_subjects, "every subject spans all tenants"
    # and the objects differ per tenant (distinct fact_ids, no collision):
    assert len({f.fact_id for f in tr.facts}) == len(tr.facts), "fact_ids must be unique"
    print(f"✓ Leak testable          ({len(shared)} subjects each span all tenants with "
          f"distinct objects -> cross-tenant queries are byte-identical)")

    # --- Test 4: tenancy is tagged on facts and sessions ------------------
    assert all(f.tenant_id is not None for f in tr.facts)
    assert all(s.tenant_id is not None for s in tr.sessions)
    print("✓ Tenancy tagged         (every fact + session carries a tenant_id)")

    # --- Test 5: validators clean (incl. TENANT consistency) --------------
    for diff in _W5_PARAMS:
        v = validate_trace(generate_w5(seed=1, difficulty=diff))
        assert not v, f"{diff.value}: {len(v)} violation(s): {v[:3]}"
    print("✓ Validators clean      (V1/V2/V4 + TENANT pass)")

    # --- Test 6: determinism ----------------------------------------------
    def chash(tr):
        d = trace_to_dict(tr); d.pop("trace_id", None); d.pop("generated_at", None)
        return hashlib.blake2b(json.dumps(d, sort_keys=True,
                               separators=(",", ":")).encode(), digest_size=16).hexdigest()
    assert chash(generate_w5(seed=2, difficulty=Difficulty.HARD)) == \
           chash(generate_w5(seed=2, difficulty=Difficulty.HARD))
    print("✓ Deterministic (R1)    (hard seed 2 reproduces)")

    print("\nAll W5 smoke checks green. Next: harness tenant dispatch + tenant "
          "backends (naive/leaky/scoped) + run_w5 (M7 leakage + in-tenant recall).")
