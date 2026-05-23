"""
GRAFOMEM W10 runner bridge — trace <-> backend <-> outcome oracle (§10).

The eval-layer oracle (aml.eval.concurrency) is pure: it scores an abstract
Outcome against a Spec. This module is the adapter that connects it to the real
world on both sides:

  1. replay_prefix    — write a W10 trace's base facts (the txn_id=None prefix)
                        into a backend, yielding fid->ref and key->base maps.
  2. extract          — turn one trace ConcurrencyGroup (+ its txn-tagged turns)
                        into the backend-facing ConcurrentGroup AND the oracle's
                        Spec, recording per-read keys and per-txn delete keys.
  3. canonicalize     — map the backend's ConcurrentResult back to an Outcome
                        (productionizes the isolation_backends smoke's _canon).
  4. evaluate_group   — submit_concurrent -> canonicalize -> evaluate against the
                        oracle, deriving committed_deletes from the OUTCOME (a
                        delete only counts if its transaction committed).

It assumes the backend round-trips WriteOptions.metadata onto the Memory it
returns (the GMP contract; the reference stores honor it) — that is how the
canonicalize recovers (subject, predicate, object) per memory.

The single-valued prefix queries are NOT scored here: W10's measurement is the
isolation outcome of the concurrent groups, not prefix recall.
"""

from __future__ import annotations

from dataclasses import dataclass

from aml.backends.interface import (
    ConcurrentGroup,
    IsolationLevel,
    OpKind,
    RetrieveOptions,
    SubmittedTxn,
    TxnOp,
    WriteOptions,
)
from aml.eval.concurrency import (
    LostUpdateSpec,
    NonRepeatableReadSpec,
    Outcome,
    WriteSkewSpec,
    evaluate_claim,
    resurrects,
)
from aml.generator.trace import Trace, TurnRole, TxnAnomaly

Key = tuple[str, str]

_RANK = {IsolationLevel.SERIALIZABLE: 0, IsolationLevel.SNAPSHOT: 1,
         IsolationLevel.READ_COMMITTED: 2}
_WORST = {"OK": 0, "DOWNGRADE": 1, "VIOLATION": 2}


@dataclass(frozen=True, slots=True)
class GroupVerdict:
    """One concurrency group's outcome against the backend's declared policy."""
    anomaly: TxnAnomaly
    status: str                       # OK | DOWNGRADE | VIOLATION
    achieved: IsolationLevel | None   # None for a §10.4 resurrection violation
    claimed: IsolationLevel
    detail: str = ""


# --- 1. prefix replay ------------------------------------------------------
def replay_prefix(backend, trace: Trace) -> tuple[dict[bytes, object], dict[Key, bytes]]:
    """Write base facts (txn_id=None introduce turns), in canonical order.
    Returns fid->backend-ref and (subject,predicate)->base-fid."""
    fact_by_id = {f.fact_id: f for f in trace.facts}
    ordered = sorted(
        ((t.timestamp, si, ti, t)
         for si, s in enumerate(trace.sessions)
         for ti, t in enumerate(s.turns) if t.txn_id is None),
        key=lambda r: (r[0], r[1], r[2]))
    fid_to_ref: dict[bytes, object] = {}
    base_fid_by_key: dict[Key, bytes] = {}
    for _ts, _si, _ti, turn in ordered:
        for fid in turn.introduces:
            f = fact_by_id[fid]
            ref = backend.write(turn.content, WriteOptions(
                valid_from=f.valid_from,
                metadata={"subject": f.subject, "predicate": f.predicate,
                          "object": f.object}))
            fid_to_ref[fid] = ref
            base_fid_by_key[(f.subject, f.predicate)] = fid
    return fid_to_ref, base_fid_by_key


# --- 2. extract trace group -> (ConcurrentGroup, Spec, read keys, delete keys) ---
def _turns_by_txn(trace: Trace) -> dict[object, list]:
    rows: dict[object, list] = {}
    for si, s in enumerate(trace.sessions):
        for ti, t in enumerate(s.turns):
            if t.txn_id is not None:
                rows.setdefault(t.txn_id, []).append((t.timestamp, si, ti, t))
    return {k: [r[3] for r in sorted(v, key=lambda r: (r[0], r[1], r[2]))]
            for k, v in rows.items()}


def extract(trace: Trace, group, fid_to_ref, base_fid_by_key):
    """Build the backend-facing ConcurrentGroup, the oracle Spec, and the maps
    canonicalize/eval need (per-txn read keys, per-txn delete key)."""
    fact_by_id = {f.fact_id: f for f in trace.facts}
    turns_by_txn = _turns_by_txn(trace)
    gk: Key = (group.subject, group.predicate)

    submitted: list[SubmittedTxn] = []
    read_keys_by_txn: dict[object, list[Key]] = {}
    delete_key_by_txn: dict[object, Key] = {}
    intro_by_txn: dict[object, tuple[Key, object]] = {}  # txn -> (key, new object)

    for tx in group.transactions:
        ops: list[TxnOp] = []
        rkeys: list[Key] = []
        for t in turns_by_txn.get(tx.txn_id, []):
            if t.role is TurnRole.AGENT_QUERY:
                # read key: requires[0]'s key (write-skew, the other key) else the
                # group key (non-repeatable read reads the group's own key)
                if t.requires:
                    rf = fact_by_id[t.requires[0]]
                    key = (rf.subject, rf.predicate)
                else:
                    key = gk
                ops.append(TxnOp(OpKind.READ,
                                 target=fid_to_ref[base_fid_by_key[key]],
                                 retrieve_options=RetrieveOptions()))
                rkeys.append(key)
            elif t.introduces:
                f = fact_by_id[t.introduces[0]]
                key = (f.subject, f.predicate)
                ops.append(TxnOp(OpKind.SUPERSEDE, content=t.content,
                                 target=fid_to_ref[base_fid_by_key[key]],
                                 write_options=WriteOptions(metadata={
                                     "subject": f.subject, "predicate": f.predicate,
                                     "object": f.object})))
                intro_by_txn[tx.txn_id] = (key, f.object)
            elif t.deletes:
                f = fact_by_id[t.deletes[0]]
                ops.append(TxnOp(OpKind.DELETE, target=fid_to_ref[t.deletes[0]]))
                delete_key_by_txn[tx.txn_id] = (f.subject, f.predicate)
        submitted.append(SubmittedTxn(tx.txn_id, ops, list(tx.depends_on)))
        read_keys_by_txn[tx.txn_id] = rkeys

    cgroup = ConcurrentGroup(subject=group.subject, predicate=group.predicate,
                             transactions=submitted)
    spec = _build_spec(group, gk, base_fid_by_key, fact_by_id, intro_by_txn,
                       [tx.txn_id for tx in group.transactions])
    return cgroup, spec, read_keys_by_txn, delete_key_by_txn


def _build_spec(group, gk, base_fid_by_key, fact_by_id, intro_by_txn, txn_ids):
    def base_obj(key: Key):
        return fact_by_id[base_fid_by_key[key]].object

    a = group.anomaly
    if a is TxnAnomaly.LOST_UPDATE:
        writers = tuple((tid, obj) for tid, (_k, obj) in intro_by_txn.items())
        return LostUpdateSpec(key=gk, base=base_obj(gk), writers=writers)
    if a is TxnAnomaly.WRITE_SKEW:
        # writer_a is the txn that writes the GROUP key; the other is writer_b.
        # (Don't rely on transactions[] order.)
        wa = next(t for t, (k, _o) in intro_by_txn.items() if k == gk)
        wb = next(t for t in intro_by_txn if t != wa)
        ka, oa = intro_by_txn[wa]
        kb, ob = intro_by_txn[wb]
        return WriteSkewSpec(key_a=ka, base_a=base_obj(ka), writer_a=wa, new_a=oa,
                             key_b=kb, base_b=base_obj(kb), writer_b=wb, new_b=ob)
    if a is TxnAnomaly.NON_REPEATABLE_READ:
        writer = next(iter(intro_by_txn))            # the only writing txn
        _k, new = intro_by_txn[writer]
        reader = next(t for t in txn_ids if t not in intro_by_txn)
        return NonRepeatableReadSpec(key=gk, base=base_obj(gk),
                                     writer=writer, new=new, reader=reader)
    if a is TxnAnomaly.RESURRECTION:
        return None                                  # §10.4: checked via resurrects()
    raise ValueError(f"unhandled anomaly {a!r}")


# --- 3. canonicalize ConcurrentResult -> Outcome ---------------------------
def _read_value(read_result, key: Key | None):
    if not read_result:
        return None
    if key is not None:
        for m in read_result:
            if (m.metadata.get("subject"), m.metadata.get("predicate")) == key:
                return m.metadata.get("object")
    return read_result[0].metadata.get("object")


def canonicalize(result, read_keys_by_txn) -> Outcome:
    by_key: dict[Key, list] = {}
    for m in sorted(result.final_state, key=lambda x: x.ref):
        k = (m.metadata["subject"], m.metadata["predicate"])
        by_key.setdefault(k, []).append(m.metadata["object"])
    chains = {k: tuple(v) for k, v in by_key.items()}
    reads: dict[object, tuple] = {}
    for tid, read_results in result.reads.items():
        rkeys = read_keys_by_txn.get(tid, [])
        reads[tid] = tuple(
            _read_value(rr, rkeys[i] if i < len(rkeys) else None)
            for i, rr in enumerate(read_results))
    return Outcome.of(chains, committed=result.committed,
                      aborted=result.aborted, reads=reads)


# --- 4. evaluate one group + run a whole trace -----------------------------
def evaluate_group(backend, trace: Trace, group,
                   fid_to_ref, base_fid_by_key) -> GroupVerdict:
    cgroup, spec, read_keys_by_txn, delete_key_by_txn = extract(
        trace, group, fid_to_ref, base_fid_by_key)
    policy = backend.declared_policy
    result = backend.submit_concurrent(cgroup, policy)
    outcome = canonicalize(result, read_keys_by_txn)

    # committed_deletes is OUTCOME-derived: a delete counts only if it committed.
    committed_deletes = frozenset(
        delete_key_by_txn[tid] for tid in result.committed
        if tid in delete_key_by_txn)

    if spec is None:  # RESURRECTION — §10.4 durability, below the lattice
        violated = resurrects(outcome, committed_deletes)
        return GroupVerdict(
            anomaly=group.anomaly,
            status="VIOLATION" if violated else "OK",
            achieved=None,                # durability is pass/fail, not a level
            claimed=policy.level,
            detail="resurrected a committed delete (§10.4)" if violated
                   else "committed delete stayed durable")

    cr = evaluate_claim(spec, outcome, policy.level, policy.conflict_rule,
                        committed_deletes=committed_deletes)
    return GroupVerdict(anomaly=group.anomaly, status=cr.status,
                        achieved=cr.achieved, claimed=cr.claimed, detail=cr.detail)


def run_w10(backend, trace: Trace) -> list[GroupVerdict]:
    fid_to_ref, base_fid_by_key = replay_prefix(backend, trace)
    return [evaluate_group(backend, trace, g, fid_to_ref, base_fid_by_key)
            for g in trace.concurrency_groups]


def summarize(verdicts: list[GroupVerdict]) -> dict:
    """Per-trace rollup: overall verdict (worst), combined achieved level (the
    weakest across isolation-anomaly groups), and per-group status."""
    overall = max((v.status for v in verdicts), key=lambda s: _WORST[s], default="OK")
    iso = [v.achieved for v in verdicts if v.achieved is not None
           and v.anomaly is not TxnAnomaly.RESURRECTION]
    combined = max(iso, key=lambda l: _RANK[l]) if iso else None
    return {"overall": overall, "combined_achieved": combined,
            "by_group": [(v.anomaly.value, v.status,
                          v.achieved.value if v.achieved else None) for v in verdicts]}


# ============================================================================
# Smoke check — run `python -m aml.eval.concurrency_runner`
# Closes the full loop on REAL generated traces: generator -> extract ->
# backend.submit_concurrent -> canonicalize -> outcome oracle.
# ============================================================================

if __name__ == "__main__":
    from aml.backends.isolation_backends import (
        NoIsolationStore, ReadCommittedStore, ResurrectingStore,
        SerializableStore, SnapshotStore,
    )
    from aml.generator.workloads.w10 import Difficulty, generate_w10

    print("GRAFOMEM eval/concurrency_runner.py — W10 trace<->backend<->oracle bridge\n")

    stores = [SerializableStore, SnapshotStore, ReadCommittedStore,
              NoIsolationStore, ResurrectingStore]
    # (overall verdict, combined achieved level) expected per store
    expect = {
        "SerializableStore":  ("OK", "serializable"),
        "SnapshotStore":      ("OK", "snapshot"),
        "ReadCommittedStore": ("OK", "read_committed"),
        "NoIsolationStore":   ("DOWNGRADE", "read_committed"),
        "ResurrectingStore":  ("VIOLATION", "serializable"),
    }

    # -- multi-seed: overall verdict + combined level + resurrection status --
    for seed in (1, 2, 3, 7):
        tr = generate_w10(seed=seed, difficulty=Difficulty.EASY)
        for cls in stores:
            s = summarize(run_w10(cls(), tr))
            eo, ec = expect[cls.__name__]
            assert s["overall"] == eo, (seed, cls.__name__, s)
            assert (s["combined_achieved"] or "") == ec, (seed, cls.__name__, s)
            res = next(g for g in run_w10(cls(), tr)
                       if g.anomaly is TxnAnomaly.RESURRECTION)
            assert res.status == ("VIOLATION" if cls is ResurrectingStore else "OK")
    print("✓ Full loop on real traces           (5 stores x 4 seeds: verdict + level + §10.4)")

    # -- the lattice is observed end-to-end (not just unit-tested) -----------
    tr = generate_w10(seed=1, difficulty=Difficulty.EASY)
    levels = {cls.__name__: summarize(run_w10(cls(), tr))["combined_achieved"]
              for cls in stores}
    assert levels["SerializableStore"] == "serializable"
    assert levels["SnapshotStore"] == "snapshot"
    assert levels["ReadCommittedStore"] == "read_committed"
    print("✓ Achieved-level lattice observed     (serializable > snapshot > read_committed)")

    # -- over-claimer caught; durability violator caught --------------------
    no_iso = summarize(run_w10(NoIsolationStore(), tr))
    assert no_iso["overall"] == "DOWNGRADE" and no_iso["combined_achieved"] == "read_committed"
    print("✓ Over-claimer downgraded             (NoIsolationStore claims serializable -> read_committed)")
    res = summarize(run_w10(ResurrectingStore(), tr))
    assert res["overall"] == "VIOLATION"
    print("✓ §10.4 violator caught               (ResurrectingStore revives a committed delete)")

    # -- scale: hard trace (32 groups) flows through the bridge -------------
    th = generate_w10(seed=2, difficulty=Difficulty.HARD)
    sh = summarize(run_w10(SnapshotStore(), th))
    assert sh["overall"] == "OK" and sh["combined_achieved"] == "snapshot"
    assert len(run_w10(SerializableStore(), th)) == 32
    print("✓ Scales to hard (32 groups)          (bridge handles full-size traces)")

    print("\nAll W10 runner-bridge smoke checks green. Generator -> backend -> oracle "
          "loop closed on real traces. Next: run_w10.py CLI + W10 conformance suite.")
