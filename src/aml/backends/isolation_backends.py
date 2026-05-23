"""
GRAFOMEM isolation backends — the concurrency spectrum (W10, §10).

Five stores, all claiming {CONCURRENCY_CONTROL, SUPERSESSION_CHAIN, AUDIT,
HARD_DELETE} and sharing one in-memory key/value base. They differ ONLY in
submit_concurrent — the isolation strategy — which is exactly the point: each
declares an IsolationPolicy, and only the outcome oracle (aml.eval.concurrency)
placing the observed result against the permissible-finals lattice tells them
apart. This is the W10 analog of the tenant spectrum (claim != behavior).

  - SerializableStore   chains same-key writes; SSI-aborts a write-skew rw-anti-
                        dependency cycle; reads are repeatable. Declares + achieves
                        serializable.
  - SnapshotStore       reads the begin snapshot (repeatable); first-committer-wins
                        on same-key writes (no lost update); admits write skew on
                        disjoint keys. Declares + achieves snapshot.
  - ReadCommittedStore  last-writer-wins (lost update); reads see latest-committed
                        (non-repeatable); admits write skew. Declares + achieves
                        read_committed.
  - NoIsolationStore    SAME behavior as read_committed, but DECLARES serializable
                        — the over-claimer (tenancy's leaky_tenant analog). The
                        oracle downgrades it (§10.5).
  - ResurrectingStore   serializable on the three anomaly probes, but a supersede
                        revives a committed delete — a §10.4 durability violation,
                        permissible at no level.

These are SYNTHETIC stores: each realizes one deterministic admissible execution
to land at its tier on the planted probes (no real threads, §10.6). They are not
persistent/vector backends — the W10 measurement is submit_concurrent, not
retrieval — so retrieve() is a minimal head-return for prefix sanity only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from aml.backends.interface import (
    Capability,
    CapabilityNotSupported,
    ConcurrentGroup,
    ConcurrentResult,
    ConflictRule,
    IsolationLevel,
    IsolationPolicy,
    Memory,
    OpKind,
    RetrieveOptions,
    SubmittedTxn,
    TxnId,
    TxnOp,
    WriteOptions,
)

__grafomem_interface__ = "0.2.0"

_DELETED = object()  # sentinel: a key whose committed state is "deleted"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class _ConcurrentStoreBase:
    """In-memory key/value store over the 7-method contract + submit_concurrent.
    A key is (subject, predicate); each key holds a chain of object VALUES. The
    five strategies override `_resolve`."""

    __grafomem_interface__ = "0.2.0"
    declared_policy: IsolationPolicy  # set per subclass

    def __init__(self) -> None:
        self._store: dict[int, Memory] = {}
        self._key_of: dict[int, tuple[str, str]] = {}
        self._value: dict[int, str] = {}            # ref -> object value
        self._chain: dict[tuple[str, str], list[int]] = {}  # key -> ordered refs
        self._deleted: set[int] = set()
        self._next = 0

    # --- base contract -----------------------------------------------------
    def capabilities(self) -> set[Capability]:
        # CONCURRENCY_CONTROL only: these are W10-spectrum demonstrators. Their
        # retrieve() is a minimal head-return (not a ranking store), so they do
        # NOT pass W2/W6 — claiming SUPERSESSION_CHAIN/HARD_DELETE/AUDIT would be
        # an over-declaration the conformance suite (rightly) flags. Concurrent
        # supersede/delete are exercised through submit_concurrent, not the base
        # retrieval contract.
        return {Capability.CONCURRENCY_CONTROL}

    def write(self, content: str, options: WriteOptions) -> int:
        meta = dict(options.metadata)
        key = (meta.get("subject"), meta.get("predicate"))
        ref = self._next
        self._next += 1
        self._store[ref] = Memory(ref=ref, content=content, written_at=_now(),
                                  metadata=meta)
        self._key_of[ref] = key
        self._value[ref] = meta.get("object")
        self._chain.setdefault(key, []).append(ref)
        return ref

    def supersede(self, old_ref, content: str, options: WriteOptions) -> int:
        new_ref = self.write(content, options)
        if old_ref in self._store:
            self._store[old_ref].superseded_by = new_ref
        return new_ref

    def delete(self, ref) -> bool:
        if ref not in self._store:
            return False
        self._deleted.add(ref)
        return True

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        # Minimal: current heads, budget-limited (prefix sanity; not the W10 metric).
        heads = [refs[-1] for refs in self._chain.values()
                 if refs and refs[-1] not in self._deleted]
        out, used = [], 0
        for ref in sorted(heads):
            m = self._store[ref]
            if used + len(m.content) > options.budget_tokens:
                break
            out.append(m)
            used += len(m.content)
        return out

    def audit(self) -> Iterator[Memory]:
        return iter([m for r, m in self._store.items() if r not in self._deleted])

    def flush(self) -> None:
        pass

    # --- DAG helpers -------------------------------------------------------
    @staticmethod
    def _topo_order(txns: list[SubmittedTxn]) -> list[TxnId]:
        deps = {t.txn_id: set(t.depends_on) for t in txns}
        order, ready = [], [t.txn_id for t in txns if not deps[t.txn_id]]
        done: set = set()
        # stable: preserve submission order among ready txns
        seq = [t.txn_id for t in txns]
        while len(order) < len(txns):
            ready = [t for t in seq if t not in done and deps[t] <= done]
            if not ready:  # cycle (validator catches this upstream); fall back
                ready = [t for t in seq if t not in done]
            t = ready[0]
            order.append(t)
            done.add(t)
        return order

    @staticmethod
    def _ancestors(tid: TxnId, deps: dict[TxnId, set]) -> set:
        seen, stack = set(), list(deps.get(tid, ()))
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack.extend(deps.get(x, ()))
        return seen

    def _concurrent(self, a: TxnId, b: TxnId, deps: dict[TxnId, set]) -> bool:
        return (b not in self._ancestors(a, deps)
                and a not in self._ancestors(b, deps))

    # --- op analysis -------------------------------------------------------
    def _analyze(self, group: ConcurrentGroup):
        """Per txn (in submission order): ordered ops as dicts, plus depends_on.
        Op key resolves from `target` (a base ref) or write metadata."""
        analyzed = {}
        for txn in group.transactions:
            ops = []
            for op in txn.ops:
                if op.kind is OpKind.READ:
                    ops.append({"kind": "read", "key": self._key_of.get(op.target)})
                elif op.kind in (OpKind.WRITE, OpKind.SUPERSEDE):
                    meta = dict(op.write_options.metadata) if op.write_options else {}
                    key = (self._key_of.get(op.target) if op.target is not None
                           else (meta.get("subject"), meta.get("predicate")))
                    ops.append({"kind": "write", "key": key,
                                "content": op.content, "value": meta.get("object")})
                elif op.kind is OpKind.DELETE:
                    ops.append({"kind": "delete", "key": self._key_of.get(op.target)})
            analyzed[txn.txn_id] = ops
        return analyzed

    def _begin(self, keys) -> dict:
        """Begin-snapshot value chain per touched key (committed base state)."""
        out = {}
        for key in keys:
            refs = [r for r in self._chain.get(key, []) if r not in self._deleted]
            out[key] = [self._value[r] for r in refs]
        return out

    @staticmethod
    def _touched(analyzed) -> set:
        return {o["key"] for ops in analyzed.values() for o in ops if o["key"]}

    # --- the eighth method -------------------------------------------------
    def submit_concurrent(self, group: ConcurrentGroup,
                          policy: IsolationPolicy) -> ConcurrentResult:
        analyzed = self._analyze(group)
        deps = {t.txn_id: set(t.depends_on) for t in group.transactions}
        order = self._topo_order(group.transactions)
        keys = self._touched(analyzed)
        begin = self._begin(keys)

        committed, aborted, final_chain, reads = self._resolve(
            analyzed, deps, order, begin, policy)

        return ConcurrentResult(
            final_state=self._materialize(final_chain),
            reads={t: [[self._mem(k, v)] for (k, v) in rs] for t, rs in reads.items()},
            committed=list(committed), aborted=list(aborted))

    def _resolve(self, analyzed, deps, order, begin, policy):
        raise NotImplementedError

    # --- materialization (ConcurrentResult.final_state) --------------------
    def _mem(self, key, value) -> Memory:
        subj, pred = key
        return Memory(ref=-1, content=f"{subj} {pred} {value}", written_at=_now(),
                      metadata={"subject": subj, "predicate": pred, "object": value})

    def _materialize(self, final_chain) -> list[Memory]:
        out, ref = [], 100_000
        for key, chain in final_chain.items():
            if chain is _DELETED or not chain:
                continue
            subj, pred = key
            mems = []
            for val in chain:
                m = Memory(ref=ref, content=f"{subj} {pred} {val}", written_at=_now(),
                           metadata={"subject": subj, "predicate": pred, "object": val})
                out.append(m)
                mems.append(m)
                ref += 1
            for i in range(len(mems) - 1):       # link the surviving chain
                mems[i].superseded_by = mems[i + 1].ref
        return out


# ============================================================================
# The spectrum
# ============================================================================

class SerializableStore(_ConcurrentStoreBase):
    """Chains same-key writes; SSI-aborts a write-skew antidependency cycle;
    repeatable reads. Achieves serializable."""

    __grafomem_adapter_metadata__ = {
        "isolation_policy": "serial; SSI abort on rw-antidependency cycle"}
    declared_policy = IsolationPolicy(
        level=IsolationLevel.SERIALIZABLE,
        conflict_rule=ConflictRule.FIRST_COMMITTER_WINS,
        coverage_guarantee=frozenset(
            {"non_repeatable_read", "lost_update", "write_skew", "phantom"}))

    def _resolve(self, analyzed, deps, order, begin, policy):
        read_keys = {t: {o["key"] for o in ops if o["kind"] == "read"}
                     for t, ops in analyzed.items()}
        write_keys = {t: {o["key"] for o in ops if o["kind"] in ("write", "delete")}
                      for t, ops in analyzed.items()}
        # SSI: abort one txn of any concurrent rw-antidependency cycle (each read
        # a key the other wrote). This is what makes write-skew avoidable.
        aborted: set = set()
        for a in order:
            for b in order:
                if a >= b or a in aborted or b in aborted:
                    continue
                if (self._concurrent(a, b, deps)
                        and (read_keys[a] & write_keys[b])
                        and (read_keys[b] & write_keys[a])):
                    aborted.add(b)                  # break the cycle
        working = {k: list(v) for k, v in begin.items()}
        reads, committed = {}, []
        for t in order:                              # serial execution
            if t in aborted:
                continue
            rs = []
            for o in analyzed[t]:
                if o["kind"] == "read":
                    cur = working.get(o["key"]) or [None]
                    rs.append((o["key"], cur[-1]))   # current head -> repeatable
                elif o["kind"] == "write":
                    if working.get(o["key"]) is _DELETED:
                        continue                     # durable delete: no resurrection (§10.4)
                    working.setdefault(o["key"], []).append(o["value"])
                elif o["kind"] == "delete":
                    working[o["key"]] = _DELETED
            if rs:
                reads[t] = rs
            committed.append(t)
        return committed, aborted, working, reads


class SnapshotStore(_ConcurrentStoreBase):
    """Begin-snapshot reads (repeatable); first-committer-wins on same-key
    writes (no lost update); admits write skew on disjoint keys. Achieves
    snapshot."""

    __grafomem_adapter_metadata__ = {
        "isolation_policy": "snapshot reads; first-committer-wins; allows write skew"}
    declared_policy = IsolationPolicy(
        level=IsolationLevel.SNAPSHOT,
        conflict_rule=ConflictRule.FIRST_COMMITTER_WINS,
        coverage_guarantee=frozenset({"non_repeatable_read", "lost_update", "phantom"}))

    def _resolve(self, analyzed, deps, order, begin, policy):
        write_keys = {t: [o["key"] for o in analyzed[t]
                          if o["kind"] in ("write", "delete")] for t in order}
        aborted, first_writer = set(), {}
        for t in order:                              # FCW on same-key conflicts
            for k in write_keys[t]:
                if k in first_writer and self._concurrent(first_writer[k], t, deps):
                    aborted.add(t)
                else:
                    first_writer.setdefault(k, t)
        working = {k: list(v) for k, v in begin.items()}
        reads, committed = {}, []
        for t in order:
            if t in aborted:
                continue
            rs = []
            for o in analyzed[t]:
                if o["kind"] == "read":
                    snap = begin.get(o["key"]) or [None]
                    rs.append((o["key"], snap[-1]))  # BEGIN snapshot -> repeatable
                elif o["kind"] == "write":
                    if working.get(o["key"]) is _DELETED:
                        continue                     # durable delete: no resurrection (§10.4)
                    working.setdefault(o["key"], []).append(o["value"])
                elif o["kind"] == "delete":
                    working[o["key"]] = _DELETED
            if rs:
                reads[t] = rs
            committed.append(t)
        return committed, aborted, working, reads


class ReadCommittedStore(_ConcurrentStoreBase):
    """Last-writer-wins (lost update); reads see latest-committed, interleaved to
    expose non-repeatable read; admits write skew. Achieves read_committed."""

    __grafomem_adapter_metadata__ = {
        "isolation_policy": "last-writer-wins; latest-committed reads (non-repeatable)"}
    declared_policy = IsolationPolicy(
        level=IsolationLevel.READ_COMMITTED,
        conflict_rule=ConflictRule.LAST_COMMITTER_WINS,
        coverage_guarantee=frozenset({"phantom"}))

    def _resolve(self, analyzed, deps, order, begin, policy):
        working = {k: list(v) for k, v in begin.items()}
        reads = {t: [] for t in order}
        committed: set = set()
        rounds = max((len(ops) for ops in analyzed.values()), default=0)
        for i in range(rounds):                      # round-robin interleave
            for t in order:
                ops = analyzed[t]
                if i >= len(ops):
                    continue
                o = ops[i]
                if o["kind"] == "read":
                    cur = working.get(o["key"]) or [None]
                    reads[t].append((o["key"], cur[-1]))   # latest committed
                elif o["kind"] == "write":
                    if working.get(o["key"]) is _DELETED:
                        continue                     # durable delete: no resurrection (§10.4)
                    base = begin.get(o["key"]) or []
                    working[o["key"]] = base[:1] + [o["value"]]  # LWW: prior write lost
                elif o["kind"] == "delete":
                    working[o["key"]] = _DELETED
                committed.add(t)
        reads = {t: rs for t, rs in reads.items() if rs}
        return [t for t in order if t in committed], [], working, reads


class NoIsolationStore(ReadCommittedStore):
    """No concurrency control (read_committed behavior) but DECLARES serializable.
    The over-claimer — the oracle downgrades it (§10.5). Tenancy's leaky_tenant."""

    __grafomem_adapter_metadata__ = {
        "isolation_policy": "NONE (last-writer-wins) but CLAIMS serializable -> downgraded"}
    declared_policy = IsolationPolicy(
        level=IsolationLevel.SERIALIZABLE,                  # the lie
        conflict_rule=ConflictRule.FIRST_COMMITTER_WINS,
        coverage_guarantee=frozenset(
            {"non_repeatable_read", "lost_update", "write_skew", "phantom"}))


class ResurrectingStore(SerializableStore):
    """Serializable on the anomaly probes, but a concurrent supersede REVIVES a
    committed delete — a §10.4 durability violation (permissible at no level)."""

    __grafomem_adapter_metadata__ = {
        "isolation_policy": "serializable, but supersede resurrects a committed delete (§10.4 violation)"}

    def _resolve(self, analyzed, deps, order, begin, policy):
        committed, aborted, working, reads = super()._resolve(
            analyzed, deps, order, begin, policy)
        # Durability bug: where a key was deleted AND superseded in the group,
        # let the supersede win — the deleted key comes back to life.
        for t in committed:
            for o in analyzed[t]:
                if o["kind"] == "write" and working.get(o["key"]) is _DELETED:
                    base = begin.get(o["key"]) or []
                    working[o["key"]] = base + [o["value"]]   # RESURRECTED
        return committed, aborted, working, reads


# ============================================================================
# Smoke check — run `python -m aml.backends.isolation_backends`
# end-to-end: build each probe, run every store, canonicalize the
# ConcurrentResult, and assert each lands at its intended tier via
# aml.eval.concurrency. (A minimal local canonicalize stands in for the
# increment-6 runner bridge.)
# ============================================================================

if __name__ == "__main__":
    from aml.backends.interface import (
        ConcurrentMemoryBackend, MemoryBackend, SubmittedTxn, TxnOp,
    )
    from aml.eval.concurrency import (
        LostUpdateSpec, NonRepeatableReadSpec, Outcome, WriteSkewSpec,
        evaluate_claim, probe_achieved_level, resurrects,
    )

    SR, SN, RC = (IsolationLevel.SERIALIZABLE, IsolationLevel.SNAPSHOT,
                  IsolationLevel.READ_COMMITTED)
    _RANK = {SR: 0, SN: 1, RC: 2}

    def _wo(s, p, o):
        return WriteOptions(metadata={"subject": s, "predicate": p, "object": o})

    def _canon(result) -> Outcome:
        by_key: dict = {}
        for m in sorted(result.final_state, key=lambda x: x.ref):
            k = (m.metadata["subject"], m.metadata["predicate"])
            by_key.setdefault(k, []).append(m.metadata["object"])
        reads = {t: tuple(rl[0].metadata["object"] for rl in rls if rl)
                 for t, rls in result.reads.items()}
        return Outcome.of({k: tuple(v) for k, v in by_key.items()},
                          committed=result.committed, aborted=result.aborted, reads=reads)

    def _lost(store):
        r0 = store.write("u city Rome", _wo("u", "city", "Rome"))
        g = ConcurrentGroup("u", "city", transactions=[
            SubmittedTxn("T1", [TxnOp(OpKind.SUPERSEDE, content="u city Milan",
                                      target=r0, write_options=_wo("u", "city", "Milan"))]),
            SubmittedTxn("T2", [TxnOp(OpKind.SUPERSEDE, content="u city Turin",
                                      target=r0, write_options=_wo("u", "city", "Turin"))])])
        return g, LostUpdateSpec(key=("u", "city"), base="Rome",
                                 writers=(("T1", "Milan"), ("T2", "Turin")))

    def _skew(store):
        ra = store.write("docA on_call yes", _wo("docA", "on_call", "yes"))
        rb = store.write("docB on_call yes", _wo("docB", "on_call", "yes"))
        g = ConcurrentGroup("docA", "on_call", transactions=[
            SubmittedTxn("T1", [TxnOp(OpKind.READ, target=rb),
                                TxnOp(OpKind.SUPERSEDE, content="docA on_call no",
                                      target=ra, write_options=_wo("docA", "on_call", "no"))]),
            SubmittedTxn("T2", [TxnOp(OpKind.READ, target=ra),
                                TxnOp(OpKind.SUPERSEDE, content="docB on_call no",
                                      target=rb, write_options=_wo("docB", "on_call", "no"))])])
        return g, WriteSkewSpec(key_a=("docA", "on_call"), base_a="yes", writer_a="T1", new_a="no",
                                key_b=("docB", "on_call"), base_b="yes", writer_b="T2", new_b="no")

    def _nrr(store):
        r0 = store.write("u plan free", _wo("u", "plan", "free"))
        g = ConcurrentGroup("u", "plan", transactions=[
            SubmittedTxn("R", [TxnOp(OpKind.READ, target=r0), TxnOp(OpKind.READ, target=r0)]),
            SubmittedTxn("W", [TxnOp(OpKind.SUPERSEDE, content="u plan pro",
                                     target=r0, write_options=_wo("u", "plan", "pro"))])])
        return g, NonRepeatableReadSpec(key=("u", "plan"), base="free", writer="W",
                                        new="pro", reader="R")

    print("GRAFOMEM isolation_backends.py — five submit_concurrent stores (§10)\n")

    # -- Protocol + capability gate -----------------------------------------
    for cls in (SerializableStore, SnapshotStore, ReadCommittedStore,
                NoIsolationStore, ResurrectingStore):
        b = cls()
        assert isinstance(b, MemoryBackend) and isinstance(b, ConcurrentMemoryBackend)
        assert Capability.CONCURRENCY_CONTROL in b.capabilities()
    print("✓ Protocol + capability gate         (all five are ConcurrentMemoryBackend)")

    # -- per-store tier placement (combined) + claim verdict ----------------
    probes = (("lost", _lost), ("skew", _skew), ("nrr", _nrr))
    expected = {  # combined achieved level, claim verdict
        SerializableStore: (SR, "OK"), SnapshotStore: (SN, "OK"),
        ReadCommittedStore: (RC, "OK"), NoIsolationStore: (RC, "DOWNGRADE")}
    for cls, (exp_level, exp_verdict) in expected.items():
        achieved, verdicts = [], set()
        for _, build in probes:
            store = cls()
            g, spec = build(store)
            res = store.submit_concurrent(g, store.declared_policy)
            out = _canon(res)
            achieved.append(probe_achieved_level(spec, out))
            verdicts.add(evaluate_claim(spec, out, store.declared_policy.level,
                                        store.declared_policy.conflict_rule).status)
        combined = max(achieved, key=lambda l: _RANK[l])
        assert combined == exp_level, f"{cls.__name__}: combined {combined} != {exp_level}"
        assert verdicts == {exp_verdict}, f"{cls.__name__}: verdicts {verdicts} != {exp_verdict}"
    print("✓ SerializableStore                  (serial/skew-abort/repeatable -> serializable, OK)")
    print("✓ SnapshotStore                      (lost=SR via FCW, skew=SN, nrr=SR -> snapshot, OK)")
    print("✓ ReadCommittedStore                 (lost-update + non-repeatable -> read_committed, OK)")
    print("✓ NoIsolationStore                   (behaves at floor, CLAIMS serializable -> DOWNGRADE)")

    # -- §10.4 durability probe ---------------------------------------------
    def _del_probe(store):
        r0 = store.write("u ssn redacted", _wo("u", "ssn", "redacted"))
        g = ConcurrentGroup("u", "ssn", transactions=[
            SubmittedTxn("W", [TxnOp(OpKind.SUPERSEDE, content="u ssn 123",
                                     target=r0, write_options=_wo("u", "ssn", "123"))]),
            SubmittedTxn("D", [TxnOp(OpKind.DELETE, target=r0)])])
        return _canon(store.submit_concurrent(g, store.declared_policy))

    dead = frozenset({("u", "ssn")})
    assert resurrects(_del_probe(ResurrectingStore()), dead) is True
    assert resurrects(_del_probe(SerializableStore()), dead) is False
    print("✓ ResurrectingStore                  (revives a committed delete -> §10.4 violation; "
          "correct store keeps it durable)")

    print("\nAll isolation-backend smoke checks green. Five stores span the achieved-level "
          "lattice; the outcome oracle places each at its tier. Runner + conformance is increment 6.")
