"""
W9 experiment: cross-session deletion — does "forget" propagate?

W6 proved single-store deletion (forget here, gone here). W9 lifts the same
two-sided check to the cross-session axis: a fact deleted in one session must
not resurface through another session of the SAME backend instance.

  1. CROSS-SESSION LEAKAGE (privacy, false-positive): a deleted fact retrieved
     from a session OTHER than the one that issued the delete. Scored as the
     fraction of queries whose retrieved set contains a fact deleted before the
     query (Check L), concentrated on the deleted-probes — which W9 places in a
     different session than the delete.

  2. OVER-DELETION (correctness, false-negative): survivors must remain
     retrievable, including across sessions. M1 recall over the survivor-probes.

Three clusters span the surface (each is N session handles over one shared store):
  - propagating    : delete removes globally -> no leak, survivors intact (correct).
  - session_local  : delete is session-scoped -> survivors intact, but the fact
                     RESURFACES in another session (claims CROSS_SESSION_PROPAGATION
                     yet leaks across sessions — the W9 finding).
  - no_propagation : does not claim CROSS_SESSION_PROPAGATION -> SKIPPED (§4.9).

Why run_w9 owns the replay. The shared harness (aml.eval.harness.run_trace)
drives ONE backend instance, and delete(ref) carries no session context, so it
cannot express "delete in B, probe in C". run_w9 therefore replays the trace in
the same canonical (timestamp, session_index, turn_index) order but dispatches
each turn to cluster[session_index]. interface.py / harness.py are untouched;
w9.py, cross_session_backends.py, and this file are purely additive.

Defaults to the stub embedder at a generous budget so the propagation map runs
in seconds with no BGE download: cross-session leakage is a structural property
(is the deleted fact present and unfiltered in the probe session?), so isolating
it from ranking/budget pressure is the honest structural check. For headline
numbers under retrieval pressure, call measure(embed_fn=real, budget=512) — the
real BGE ranks the deleted fact's own question to the top, so the leak shows at
W6's budget too. Requires grafomem[backends] for the real embedder.
"""

from __future__ import annotations

from statistics import mean

from aml.backends.cross_session_backends import make_cluster
from aml.backends.interface import Capability, RetrieveOptions, WriteOptions
from aml.eval.metrics import _targets_by_turn
from aml.generator.trace import Difficulty, TurnRole
from aml.generator.workloads.w9 import generate_w9

SEEDS = range(5)
DIFF = Difficulty.HARD
CLUSTERS = ("propagating", "session_local", "no_propagation")


def _ordered_rows(trace):
    """Canonical (timestamp, session_index, turn_index) order, keeping the
    session index so each turn can be dispatched to its session handle."""
    rows = []
    for si, s in enumerate(trace.sessions):
        for ti, t in enumerate(s.turns):
            rows.append((t.timestamp, si, ti, t))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return rows


def run_cluster(cluster, trace, *, budget_tokens):
    """Per-session dispatch replay.

    Mirrors aml.eval.harness.run_trace (write -> record ref->fact_ids; delete
    iff issued; query -> flush + retrieve -> map refs back to fact_ids) but
    routes every operation to cluster[session_index]. Refs are global (the
    shared store assigns them), so a write via one session, a delete via
    another, and a probe via a third all line up on the same ref.
    """
    fact_by_id = {f.fact_id: f for f in trace.facts}
    ref_to_fids: dict[object, set[bytes]] = {}
    fid_to_ref: dict[bytes, object] = {}
    per_query: list[tuple[str, set[bytes]]] = []

    for _ts, si, _ti, turn in _ordered_rows(trace):
        h = cluster[si]
        for fid in turn.introduces:
            f = fact_by_id.get(fid)
            opts = WriteOptions(
                metadata=({"subject": f.subject, "predicate": f.predicate}
                          if f else {}),
            )
            ref = h.write(turn.content, opts)
            ref_to_fids[ref] = ref_to_fids.get(ref, set()) | {fid}
            fid_to_ref[fid] = ref
        for fid in turn.deletes:
            r = fid_to_ref.get(fid)
            if r is not None:
                h.delete(r)
        if turn.role == TurnRole.AGENT_QUERY:
            h.flush()
            mems = h.retrieve(turn.content, RetrieveOptions(budget_tokens=budget_tokens))
            retrieved: set[bytes] = set()
            for m in mems:
                retrieved |= ref_to_fids.get(m.ref, set())
            per_query.append((str(turn.turn_id), retrieved))
    return per_query


def measure(embed_fn=None, seeds=SEEDS, budget=1 << 20, diff=DIFF):
    if embed_fn is None:
        from aml.backends.vector_only import _stub_embedder
        embed_fn = _stub_embedder()
    leak = {c: [] for c in CLUSTERS}
    surv = {c: [] for c in CLUSTERS}
    skipped: dict[str, str] = {}
    for s in seeds:
        tr = generate_w9(seed=s, difficulty=diff)
        n_sess = len(tr.sessions)
        tgt = _targets_by_turn(tr)
        tsb = {str(t.turn_id): t.timestamp
               for ss in tr.sessions for t in ss.turns}
        deleted = tr.ground_truth.deleted_facts          # {fid: deleted_at}
        for c in CLUSTERS:
            cluster = make_cluster(c, n_sess, embed_fn)
            if Capability.CROSS_SESSION_PROPAGATION not in cluster[0].capabilities():
                skipped[c] = "no CROSS_SESSION_PROPAGATION"
                continue
            pq = run_cluster(cluster, tr, budget_tokens=budget)
            leak_flags, recalls = [], []
            for tid, retrieved in pq:
                ts = tsb[tid]
                deleted_before = {fid for fid, dt in deleted.items() if dt <= ts}
                leak_flags.append(1.0 if (retrieved & deleted_before) else 0.0)
                T = tgt.get(tid, set())
                if T:                                    # survivor-probe
                    recalls.append(len(retrieved & T) / len(T))
            leak[c].append(mean(leak_flags))
            surv[c].append(mean(recalls) if recalls else float("nan"))
    return leak, surv, skipped


def _verdict(lk, sv, eps=5e-3):
    leaks = lk > eps
    overdel = sv < 1.0 - eps
    if not leaks and not overdel:
        return "PROPAGATES (clean)"
    if leaks and overdel:
        return "LEAKS x-session + OVER-DELETES"
    return "LEAKS x-session" if leaks else "OVER-DELETES"


def main(embed_fn=None):
    leak, surv, skipped = measure(embed_fn)
    n = len(list(SEEDS))
    print(f"\nW9 cross-session deletion (hard, {n} seeds, stub embedder, "
          f"generous budget):\n")
    print(f"  {'cluster':16s} {'x-session leak':>14s} {'survivor recall':>16s}   verdict")
    print("  " + "-" * 74)
    for c in CLUSTERS:
        if c in skipped:
            print(f"  {c:16s} {'—':>14s} {'—':>16s}   SKIPPED ({skipped[c]})")
            continue
        lk, sv = mean(leak[c]), mean(surv[c])
        print(f"  {c:16s} {lk:13.3f}  {sv:15.3f}   {_verdict(lk, sv)}")
    print("  " + "-" * 74)
    print("  Only propagating is clean on both axes. session_local keeps survivors\n"
          "  yet leaks the deleted fact when probed from another session — it CLAIMS\n"
          "  CROSS_SESSION_PROPAGATION but does not honor it. no_propagation makes no\n"
          "  claim, so it is skipped rather than scored (§4.9).")

    # --- self-check: the map must come out as designed --------------------
    assert "no_propagation" in skipped, "no_propagation must be skipped"
    assert mean(leak["propagating"]) <= 5e-3, "propagating leaked across sessions"
    assert mean(surv["propagating"]) >= 1 - 5e-3, "propagating over-deleted"
    assert mean(leak["session_local"]) > 5e-3, "session_local should leak x-session"
    assert mean(surv["session_local"]) >= 1 - 5e-3, "session_local should keep survivors"
    print("\n✓ Propagation map as designed "
          "(propagating clean; session_local leaks x-session; no_propagation skipped).")


if __name__ == "__main__":
    main()
