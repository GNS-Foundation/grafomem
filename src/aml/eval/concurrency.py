"""
GRAFOMEM W10 outcome oracle — eval-layer, set-valued, backend-dependent (§10).

This is NOT the trace-side oracle. `aml.generator.oracle.derive_ground_truth`
answers a single-valued question — "given a trace with a total order, what is
THE ground truth?" — and is correct for W1-W9 because the order is fixed.

W10 has no such fixed answer. The permissible outcomes of a concurrency group
depend on the isolation level the *backend* declares (§10.2), which the trace
does not and must not know. So W10 ground truth is computed at EVAL time,
against the store under test — here, not in the oracle. The generator still
calls `derive_ground_truth` on the sequential prefix; the concurrent groups
live in `trace.concurrency_groups` and are evaluated by this module.

Model (option A — per-anomaly closed form). The three planted anomalies are
textbook patterns. Rather than enumerate every outcome set by hand, we encode
§10.2 as an anomaly LATTICE and derive permissibility from it:

    permissible(level) = { outcome : anomalies(outcome) ⊆ allowed(level) }

    allowed(read_committed) = {non_repeatable_read, lost_update, write_skew, phantom}
    allowed(snapshot)       = {write_skew, phantom}
    allowed(serializable)   = {}

Two consequences worth internalizing (both correct, both load-bearing):

  * An anomaly-FREE outcome is permissible at every level. An abort (first-
    committer-wins drops the loser) introduces no anomaly, so it is permissible
    even at serializable. Therefore the lost-update probe's permissible set is
    identical for serializable and snapshot — that probe separates
    read_committed from {snapshot, serializable}, and only the write-skew probe
    separates serializable from snapshot.

  * A probe yields an UPPER BOUND on a store's achieved level (the strongest
    level whose allowed-set covers what the store exhibited). A store's overall
    achieved level (§10.5) is the WEAKEST upper bound across all its probes —
    see `combine_achieved`.

Durable delete (§10.4) sits BELOW the lattice: a committed delete that a later
op resurrects is permissible at NO level. It is a hard violation, checked
separately from the anomaly classification.

The runner adapts a backend's `interface.ConcurrentResult` into an `Outcome`
(the `canonicalize` bridge lives with the runner, increment 6, since it reads
`interface.Memory`); the GroupSpec is extracted from `trace.ConcurrencyGroup` +
its turns/facts (the extractor is the generator's concern, increment 4). This
module consumes the abstract `Outcome`/`*Spec` so it stays testable in
isolation, with no dependency on backends or generated traces.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aml.generator.trace import TxnAnomaly
from aml.backends.interface import ConflictRule, IsolationLevel


# A contended key: (subject, predicate). A key's committed state is the ordered
# tuple of values forming its supersession chain (base .. head).
Key = tuple[str, str]


class IncoherentPolicy(Exception):
    """A declared (level, conflict_rule) pair that cannot coexist (§10.1/§10.2).
    e.g. snapshot + last_committer_wins: LWW on a contended key IS a lost
    update, which snapshot excludes by definition — there is no test to run."""


class DurableDeleteViolation(Exception):
    """A committed delete was resurrected (§10.4). Used by the construction-time
    guard; at eval time resurrection is reported as a hard violation, not raised."""


# ============================================================================
# Outcome — the canonical, hashable normal form of a ConcurrentResult
# ============================================================================

@dataclass(frozen=True)
class Outcome:
    """One outcome of a concurrency group, in eval normal form.

      chains    : per live key, its committed value chain (base .. head)
      deleted   : keys whose state is "committed-deleted, still absent"
      committed : txn ids the store reported committed
      aborted   : txn ids the store reported aborted
      reads     : per reader txn, the ordered values it observed

    Hashable, so permissible sets are real sets and membership is O(1). The
    runner builds one of these from a backend's ConcurrentResult and asks
    whether it is permissible for the declared level.
    """
    chains: frozenset[tuple[Key, tuple[str, ...]]] = frozenset()
    deleted: frozenset[Key] = frozenset()
    committed: frozenset[str] = frozenset()
    aborted: frozenset[str] = frozenset()
    reads: frozenset[tuple[str, tuple[str, ...]]] = frozenset()

    @staticmethod
    def of(chains=None, *, committed=(), aborted=(), reads=None, deleted=()) -> "Outcome":
        return Outcome(
            chains=frozenset((k, tuple(v)) for k, v in (chains or {}).items()),
            deleted=frozenset(deleted),
            committed=frozenset(committed),
            aborted=frozenset(aborted),
            reads=frozenset((t, tuple(r)) for t, r in (reads or {}).items()),
        )

    def chain_of(self, key: Key) -> tuple[str, ...]:
        return dict(self.chains).get(key, ())

    def reads_of(self, txn: str) -> tuple[str, ...]:
        return dict(self.reads).get(txn, ())

    def live_keys(self) -> frozenset[Key]:
        return frozenset(k for (k, _) in self.chains)


# ============================================================================
# Group specs — the trace-side parameters of one planted anomaly probe
# ============================================================================

@dataclass(frozen=True)
class LostUpdateSpec:
    """Two concurrent writers to one key. T1 supersedes key->a, T2 key->b."""
    key: Key
    base: str
    writers: tuple[tuple[str, str], ...]  # ((txn_id, new_value), (txn_id, new_value))

    anomaly = TxnAnomaly.LOST_UPDATE


@dataclass(frozen=True)
class WriteSkewSpec:
    """Two concurrent writers to two keys, with a cross-fact invariant that is
    violated exactly when BOTH writers flip their key. (The invariant lives in
    the spec, not in a simulator — this is why option B collapses into A.)"""
    key_a: Key
    base_a: str
    writer_a: str
    new_a: str
    key_b: Key
    base_b: str
    writer_b: str
    new_b: str

    anomaly = TxnAnomaly.WRITE_SKEW


@dataclass(frozen=True)
class NonRepeatableReadSpec:
    """A reader (reads `key` twice) concurrent with a writer (key->new)."""
    key: Key
    base: str
    writer: str
    new: str
    reader: str

    anomaly = TxnAnomaly.NON_REPEATABLE_READ


Spec = LostUpdateSpec | WriteSkewSpec | NonRepeatableReadSpec


# ============================================================================
# §10.2 anomaly lattice
# ============================================================================

ALLOWED: dict[IsolationLevel, frozenset[TxnAnomaly]] = {
    IsolationLevel.READ_COMMITTED: frozenset({
        TxnAnomaly.NON_REPEATABLE_READ, TxnAnomaly.LOST_UPDATE,
        TxnAnomaly.WRITE_SKEW, TxnAnomaly.PHANTOM,
    }),
    IsolationLevel.SNAPSHOT: frozenset({TxnAnomaly.WRITE_SKEW, TxnAnomaly.PHANTOM}),
    IsolationLevel.SERIALIZABLE: frozenset(),
}

# Strongest -> weakest. Achieved level = first whose allowed-set covers the
# exhibited anomalies.
_LATTICE: tuple[IsolationLevel, ...] = (
    IsolationLevel.SERIALIZABLE, IsolationLevel.SNAPSHOT, IsolationLevel.READ_COMMITTED,
)
_RANK = {level: i for i, level in enumerate(_LATTICE)}  # smaller = stronger


# ============================================================================
# Classification & permissibility
# ============================================================================

def classify_anomalies(spec: Spec, o: Outcome) -> frozenset[TxnAnomaly]:
    """Which §10.2 anomalies does `o` exhibit, for this probe?"""
    if isinstance(spec, LostUpdateSpec):
        chain = o.chain_of(spec.key)
        # Lost update: a writer the store *committed* whose value is absent from
        # the surviving chain — silently overwritten. An aborted writer is fine.
        for tid, val in spec.writers:
            if tid in o.committed and val not in chain:
                return frozenset({TxnAnomaly.LOST_UPDATE})
        return frozenset()

    if isinstance(spec, WriteSkewSpec):
        both = spec.writer_a in o.committed and spec.writer_b in o.committed
        flipped_a = spec.new_a in o.chain_of(spec.key_a)
        flipped_b = spec.new_b in o.chain_of(spec.key_b)
        if both and flipped_a and flipped_b:  # joint invariant violated
            return frozenset({TxnAnomaly.WRITE_SKEW})
        return frozenset()

    if isinstance(spec, NonRepeatableReadSpec):
        if len(set(o.reads_of(spec.reader))) > 1:  # two reads disagree
            return frozenset({TxnAnomaly.NON_REPEATABLE_READ})
        return frozenset()

    raise TypeError(f"unknown spec type: {type(spec).__name__}")


def resurrects(o: Outcome, committed_deletes: frozenset[Key]) -> bool:
    """§10.4: did a committed delete come back to life as a live key?"""
    return bool(committed_deletes & o.live_keys())


def check_policy_coherence(level: IsolationLevel, conflict_rule: ConflictRule) -> None:
    """Raise IncoherentPolicy for declared pairs that contradict themselves."""
    if (level in (IsolationLevel.SNAPSHOT, IsolationLevel.SERIALIZABLE)
            and conflict_rule == ConflictRule.LAST_COMMITTER_WINS):
        raise IncoherentPolicy(
            f"{level.value} + last_committer_wins is self-contradictory: "
            f"silently overwriting a contended key is a lost update, which "
            f"{level.value} excludes (§10.2). Flag at declaration; no test to run."
        )


def is_permissible(
    spec: Spec, o: Outcome, level: IsolationLevel,
    *, committed_deletes: frozenset[Key] = frozenset(),
) -> bool:
    """Is `o` an outcome the store is allowed to produce at `level`?"""
    if resurrects(o, committed_deletes):
        return False  # §10.4 hard violation — below the lattice, never permitted
    return classify_anomalies(spec, o) <= ALLOWED[level]


def probe_achieved_level(
    spec: Spec, o: Outcome,
    *, committed_deletes: frozenset[Key] = frozenset(),
) -> IsolationLevel | None:
    """The strongest level consistent with `o` on THIS probe (an upper bound on
    the store's true level), or None if `o` is a hard violation (§10.4)."""
    if resurrects(o, committed_deletes):
        return None
    anomalies = classify_anomalies(spec, o)
    for level in _LATTICE:  # strongest first
        if anomalies <= ALLOWED[level]:
            return level
    return None  # exhibits an anomaly outside every level (shouldn't happen)


def combine_achieved(levels) -> IsolationLevel | None:
    """A store's overall achieved level (§10.5) is the WEAKEST per-probe upper
    bound. None if any probe was a hard violation."""
    levels = list(levels)
    if any(l is None for l in levels):
        return None
    if not levels:
        return None
    return max(levels, key=lambda l: _RANK[l])  # weakest = largest rank


@dataclass(frozen=True)
class ClaimResult:
    status: str                       # "OK" | "DOWNGRADE" | "VIOLATION"
    achieved: IsolationLevel | None
    claimed: IsolationLevel
    detail: str = ""


def evaluate_claim(
    spec: Spec, observed: Outcome, claimed_level: IsolationLevel,
    conflict_rule: ConflictRule,
    *, committed_deletes: frozenset[Key] = frozenset(),
) -> ClaimResult:
    """§10.5: compare a backend's claimed level against what `observed` proves
    on this probe. DOWNGRADE if it achieved something weaker than it claimed."""
    check_policy_coherence(claimed_level, conflict_rule)
    achieved = probe_achieved_level(spec, observed, committed_deletes=committed_deletes)
    if achieved is None:
        return ClaimResult("VIOLATION", None, claimed_level,
                           "resurrected a committed delete (§10.4)")
    if _RANK[achieved] > _RANK[claimed_level]:
        return ClaimResult("DOWNGRADE", achieved, claimed_level,
                           f"claimed {claimed_level.value}, achieved only "
                           f"{achieved.value} (§10.5)")
    return ClaimResult("OK", achieved, claimed_level)


# ============================================================================
# permissible_finals — the explicit set, for the runner's membership check and
# for documentation. Built by filtering the probe's candidate universe through
# the lattice, so it stays consistent with classify_anomalies by construction.
# ============================================================================

def _candidates(spec: Spec) -> set[Outcome]:
    """The finite universe of plausible outcomes for a probe (anomaly-free AND
    anomalous), which permissible_finals filters by level."""
    if isinstance(spec, LostUpdateSpec):
        (t1, a), (t2, b) = spec.writers
        k, v0 = spec.key, spec.base
        both = (t1, t2)
        return {
            Outcome.of({k: (v0, a, b)}, committed=both),            # chained, anomaly-free
            Outcome.of({k: (v0, b, a)}, committed=both),            # chained, anomaly-free
            Outcome.of({k: (v0, a)}, committed=(t1,), aborted=(t2,)),  # FCW, T2 aborted
            Outcome.of({k: (v0, b)}, committed=(t2,), aborted=(t1,)),  # FCW, T1 aborted
            Outcome.of({k: (v0, a)}, committed=both),               # b LOST UPDATE
            Outcome.of({k: (v0, b)}, committed=both),               # a LOST UPDATE
        }
    if isinstance(spec, WriteSkewSpec):
        ka, kb = spec.key_a, spec.key_b
        wa, wb = spec.writer_a, spec.writer_b
        return {
            # Serializable (SSI) breaks the rw-antidependency by ABORTING one
            # writer: the committed one's write takes effect, the loser's key
            # stays at base. Anomaly-free -> permissible at every level.
            # (committed => the op took effect; there is no "committed but my
            # supersede did nothing" state in GMP's model.)
            Outcome.of({ka: (spec.base_a, spec.new_a), kb: (spec.base_b,)},
                       committed=(wa,), aborted=(wb,)),
            Outcome.of({ka: (spec.base_a,), kb: (spec.base_b, spec.new_b)},
                       committed=(wb,), aborted=(wa,)),
            # Snapshot lets both commit on disjoint keys -> joint invariant
            # violated. WRITE SKEW.
            Outcome.of({ka: (spec.base_a, spec.new_a),
                        kb: (spec.base_b, spec.new_b)}, committed=(wa, wb)),
        }
    if isinstance(spec, NonRepeatableReadSpec):
        k, final = spec.key, {spec.key: (spec.base, spec.new)}
        wr = (spec.writer, spec.reader)
        return {
            Outcome.of(final, committed=wr, reads={spec.reader: (spec.base, spec.base)}),  # repeatable
            Outcome.of(final, committed=wr, reads={spec.reader: (spec.new, spec.new)}),    # repeatable
            Outcome.of(final, committed=wr, reads={spec.reader: (spec.base, spec.new)}),   # NON-REPEATABLE
        }
    raise TypeError(f"unknown spec type: {type(spec).__name__}")


def permissible_finals(
    spec: Spec, level: IsolationLevel,
    *, committed_deletes: frozenset[Key] = frozenset(),
) -> set[Outcome]:
    """An ILLUSTRATIVE set of permissible outcomes for this probe at `level`,
    for documentation and smoke tests — the probe's hand-listed candidate
    universe filtered through the lattice.

    It is NOT exhaustive: a backend may legitimately produce an anomaly-free
    outcome not enumerated in `_candidates` (a different abort split, a both-
    abort, a different chain order). The runner's authoritative conformance
    check is therefore `is_permissible(spec, observed, level)`, which classifies
    the actual observed outcome and is candidate-independent — NOT membership in
    this set."""
    return {o for o in _candidates(spec)
            if is_permissible(spec, o, level, committed_deletes=committed_deletes)}


# ============================================================================
# Smoke check — `python -m aml.eval.concurrency`
# ============================================================================

if __name__ == "__main__":
    RC, SN, SR = (IsolationLevel.READ_COMMITTED, IsolationLevel.SNAPSHOT,
                  IsolationLevel.SERIALIZABLE)
    print("GRAFOMEM eval/concurrency.py — W10 outcome oracle (set-valued, §10)\n")

    # -- Probe 1: lost update -----------------------------------------------
    lu = LostUpdateSpec(key=("u", "city"), base="Rome",
                        writers=(("T1", "Milan"), ("T2", "Turin")))
    chained = Outcome.of({("u", "city"): ("Rome", "Milan", "Turin")}, committed=("T1", "T2"))
    fcw = Outcome.of({("u", "city"): ("Rome", "Milan")}, committed=("T1",), aborted=("T2",))
    lost = Outcome.of({("u", "city"): ("Rome", "Milan")}, committed=("T1", "T2"))
    assert classify_anomalies(lu, chained) == frozenset()
    assert classify_anomalies(lu, fcw) == frozenset()
    assert classify_anomalies(lu, lost) == frozenset({TxnAnomaly.LOST_UPDATE})
    assert is_permissible(lu, lost, RC)
    assert not is_permissible(lu, lost, SN) and not is_permissible(lu, lost, SR)
    assert permissible_finals(lu, SR) == permissible_finals(lu, SN)
    assert lost in permissible_finals(lu, RC) and lost not in permissible_finals(lu, SR)
    assert probe_achieved_level(lu, chained) == SR
    assert probe_achieved_level(lu, fcw) == SR
    assert probe_achieved_level(lu, lost) == RC
    print("✓ lost update     classify+lattice  "
          f"(lost-update ∈ RC only; perm(SR)==perm(SN)={len(permissible_finals(lu, SR))} outcomes)")

    # -- Probe 2: write skew ------------------------------------------------
    ws = WriteSkewSpec(key_a=("docA", "on_call"), base_a="yes", writer_a="T1", new_a="no",
                       key_b=("docB", "on_call"), base_b="yes", writer_b="T2", new_b="no")
    skew = Outcome.of({("docA", "on_call"): ("yes", "no"),
                       ("docB", "on_call"): ("yes", "no")}, committed=("T1", "T2"))
    only_a = Outcome.of({("docA", "on_call"): ("yes", "no"),
                         ("docB", "on_call"): ("yes",)}, committed=("T1",), aborted=("T2",))
    assert classify_anomalies(ws, skew) == frozenset({TxnAnomaly.WRITE_SKEW})
    assert classify_anomalies(ws, only_a) == frozenset()
    assert is_permissible(ws, skew, SN) and is_permissible(ws, skew, RC)
    assert not is_permissible(ws, skew, SR)
    assert probe_achieved_level(ws, skew) == SN and probe_achieved_level(ws, only_a) == SR
    assert only_a in permissible_finals(ws, SR)        # the SSI-abort outcome
    assert permissible_finals(ws, SR) != permissible_finals(ws, SN)
    print("✓ write skew      classify+lattice  "
          "(skew ∈ SN, ∉ SR; this probe separates SR from SN)")

    # -- Probe 3: non-repeatable read ---------------------------------------
    nr = NonRepeatableReadSpec(key=("u", "plan"), base="free", writer="T2", new="pro", reader="T1")
    diff = Outcome.of({("u", "plan"): ("free", "pro")}, committed=("T1", "T2"),
                      reads={"T1": ("free", "pro")})
    same = Outcome.of({("u", "plan"): ("free", "pro")}, committed=("T1", "T2"),
                      reads={"T1": ("pro", "pro")})
    assert classify_anomalies(nr, diff) == frozenset({TxnAnomaly.NON_REPEATABLE_READ})
    assert classify_anomalies(nr, same) == frozenset()
    assert is_permissible(nr, diff, RC)
    assert not is_permissible(nr, diff, SN) and not is_permissible(nr, diff, SR)
    assert probe_achieved_level(nr, diff) == RC and probe_achieved_level(nr, same) == SR
    print("✓ non-repeatable  classify+lattice  (differing reads ∈ RC only)")

    # -- §10.5 downgrade + combine ------------------------------------------
    r = evaluate_claim(lu, lost, SR, ConflictRule.FIRST_COMMITTER_WINS)
    assert r.status == "DOWNGRADE" and r.achieved == RC
    r2 = evaluate_claim(ws, skew, SN, ConflictRule.FIRST_COMMITTER_WINS)
    assert r2.status == "OK" and r2.achieved == SN
    r3 = evaluate_claim(ws, skew, SR, ConflictRule.FIRST_COMMITTER_WINS)
    assert r3.status == "DOWNGRADE" and r3.achieved == SN
    assert combine_achieved([probe_achieved_level(lu, chained),
                             probe_achieved_level(ws, skew)]) == SN
    print("✓ §10.5 downgrade "
          "(claim SR + lost-update -> RC; claim SR + skew -> SN; combine -> weakest)")

    # -- §10.4 durable delete -----------------------------------------------
    dead = frozenset({("u", "ssn")})
    revived = Outcome.of({("u", "ssn"): ("redacted", "123-45-6789")}, committed=("T1",))
    assert not is_permissible(lu, revived, RC, committed_deletes=dead)
    assert not is_permissible(lu, revived, SR, committed_deletes=dead)
    assert probe_achieved_level(lu, revived, committed_deletes=dead) is None
    print("✓ §10.4 durable   (resurrected committed delete permissible at NO level)")

    # -- coherence ----------------------------------------------------------
    for lvl in (SN, SR):
        try:
            check_policy_coherence(lvl, ConflictRule.LAST_COMMITTER_WINS)
            raise AssertionError("expected IncoherentPolicy")
        except IncoherentPolicy:
            pass
    check_policy_coherence(RC, ConflictRule.LAST_COMMITTER_WINS)
    print("✓ coherence       (snapshot/serializable + LWW flagged at declaration)")

    print("\nAll W10 outcome-oracle smoke checks green. oracle.py untouched; "
          "this is eval-layer and backend-aware.")
