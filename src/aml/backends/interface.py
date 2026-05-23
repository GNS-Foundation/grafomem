"""
GRAFOMEM backend interface — v0.1.1.

Implements 02-backend-interface.md: the `MemoryBackend` Protocol that every
architecture-under-test implements, the capability-flag system, the core
types, the exception classes, and the canonical Ed25519 provenance verifier.

Design anchors (doc 02 §2):
  B1  capabilities are declared, not inferred — the harness adapts to the
      declaration and never penalizes honest omissions.
  B3  the interface is minimal but sufficient — seven methods in the v0.1
      core. v0.2 adds an eighth, submit_concurrent, gated by CONCURRENCY_CONTROL
      on the ConcurrentMemoryBackend extension (§10); the v0.1 core is unchanged.
  B4  hard delete is honest deletion — a deleted ref is unrecoverable via any
      surface; a soft-delete shadow disqualifies the HARD_DELETE claim.
  B5  MemoryRef is opaque to the harness — refs are pass-through tokens whose
      only operation is equality. Concretely typed as Any here; adapters use
      whatever ref type they like (UUID, content hash, path...).
  B6  cryptographic primitives are first-class but optional.

Adapters set two module/class attributes for the harness to read:
  __grafomem_interface__        = "0.1.1"   (target interface version)
  __grafomem_adapter_metadata__ = {...}     (non-reference adapters only, §9.2)
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable
from uuid import UUID

INTERFACE_VERSION = "0.1.1"
INTERFACE_VERSION_V2 = "0.2.0"  # backends implementing the §10 concurrency extension


# ============================================================================
# Capability flags (§3) — enumerated, independent, append-only across versions
# ============================================================================

class Capability(StrEnum):
    BI_TEMPORAL = "bi_temporal"
    HARD_DELETE = "hard_delete"
    SUPERSESSION_CHAIN = "supersession_chain"
    CROSS_SESSION_PROPAGATION = "cross_session_propagation"
    MULTI_TENANT = "multi_tenant"
    CONFLICT_DETECTION = "conflict_detection"
    PROVENANCE = "provenance"
    CRYPTOGRAPHIC_PROVENANCE = "cryptographic_provenance"
    AUDIT = "audit"
    CONCURRENCY_CONTROL = "concurrency_control"  # v0.2; gates submit_concurrent (§10)


# ============================================================================
# Core types (§4)
# ============================================================================

# Opaque to the harness (B5). Adapters substitute their own concrete ref type;
# exported so adapter authors can parametrize their own generics if they wish.
MemoryRef = TypeVar("MemoryRef")


@dataclass(slots=True)
class SourceMeta:
    """Provenance metadata attached to a retrieved Memory.

    write_id / written_at / written_by are populated under PROVENANCE.
    signature / public_key are populated under CRYPTOGRAPHIC_PROVENANCE, where
    `signature` is an Ed25519 signature over the 16-byte fact_id (§4.1).
    """
    write_id: str | None = None
    written_at: datetime | None = None
    written_by: str | None = None
    signature: bytes | None = None          # Ed25519 over 16-byte fact_id
    public_key: bytes | None = None         # Ed25519 public key (32 bytes)


@dataclass(slots=True)
class Memory:
    """A retrieved memory. Optional fields are None when the corresponding
    capability is not claimed by the backend."""
    ref: Any                                # opaque MemoryRef (B5)
    content: str
    written_at: datetime                    # backend's record of the write time
    metadata: dict = field(default_factory=dict)
    valid_from: datetime | None = None      # if BI_TEMPORAL
    valid_until: datetime | None = None     # if BI_TEMPORAL
    tenant_id: str | None = None            # if MULTI_TENANT
    superseded_by: Any | None = None        # if SUPERSESSION_CHAIN
    source: SourceMeta | None = None        # if PROVENANCE / CRYPTOGRAPHIC_PROVENANCE


@dataclass(slots=True)
class WriteOptions:
    valid_from: datetime | None = None      # honored if BI_TEMPORAL
    tenant_id: str | None = None            # honored if MULTI_TENANT
    signing_key: bytes | None = None        # if set, backend MUST sign (§4.1)
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class RetrieveOptions:
    # Hard cap on total returned content (token-proxy = characters unless the
    # backend tokenizes). The harness always sets this explicitly per the
    # tokens-per-correct-fact metric; the generous default is for conformance
    # tests and ad-hoc use where budget pressure is not under test.
    budget_tokens: int = 1 << 30
    as_of: datetime | None = None           # honored if BI_TEMPORAL; default = now
    tenant_id: str | None = None            # honored if MULTI_TENANT
    top_k: int | None = None                # non-contractual hint


# ============================================================================
# Concurrency types (§10) — v0.2, gated by CONCURRENCY_CONTROL
# ============================================================================

# A caller-assigned transaction correlation key — a UUID carried through from the
# trace's Transaction.txn_id, or a str for hand-built groups (tests / conformance
# probes). Opaque to the store, which echoes it back unchanged in
# ConcurrentResult.committed/aborted; it is NOT a store-generated ref (cf.
# MemoryRef / B5 — that one the store owns; this one the runner owns).
TxnId = str | UUID


class IsolationLevel(StrEnum):
    READ_COMMITTED = "read_committed"   # observable floor under immediate-commit (§10.2)
    SNAPSHOT = "snapshot"
    SERIALIZABLE = "serializable"


class ConflictRule(StrEnum):
    FIRST_COMMITTER_WINS = "first_committer_wins"
    LAST_COMMITTER_WINS = "last_committer_wins"
    ABORT_BOTH = "abort_both"
    MERGE = "merge"


@dataclass(slots=True)
class IsolationPolicy:
    """Declared by a CONCURRENCY_CONTROL store (§10.1). `level` and `conflict_rule`
    fix the permissible-outcome set; `conflict_rule` MUST resolve both write-write
    and write-delete conflicts (§10.1). `coverage_guarantee` is the agent-readable
    set of anomaly names the store claims to exclude (§10.5; names mirror
    trace.TxnAnomaly values), against which the suite reports claimed-vs-achieved."""
    level: IsolationLevel
    conflict_rule: ConflictRule
    coverage_guarantee: frozenset[str] = frozenset()


class OpKind(StrEnum):
    WRITE = "write"
    SUPERSEDE = "supersede"
    DELETE = "delete"
    READ = "read"


@dataclass(slots=True)
class TxnOp:
    """One operation inside a submitted transaction. `content`+`write_options`
    carry a WRITE/SUPERSEDE payload; `target` is the old ref for SUPERSEDE/DELETE;
    `query`+`retrieve_options` a READ (used to expose non-repeatable reads)."""
    kind: OpKind
    content: str | None = None
    target: Any | None = None
    query: str | None = None
    write_options: WriteOptions | None = None
    retrieve_options: RetrieveOptions | None = None


@dataclass(slots=True)
class SubmittedTxn:
    """A transaction in a concurrent group: an ordered op list plus its
    happens-before predecessors. Transactions incomparable in the DAG are
    concurrent (§4.10)."""
    txn_id: TxnId
    ops: list[TxnOp] = field(default_factory=list)
    depends_on: list[TxnId] = field(default_factory=list)


@dataclass(slots=True)
class ConcurrentGroup:
    """A contended-key group submitted to a store under one IsolationPolicy.
    `subject`/`predicate` anchor the primary contended key; individual ops may
    touch a related key (e.g. the second fact of a write-skew invariant).

    Distinct from trace.ConcurrencyGroup (note the near-identical name): that is
    the trace-level *spec* structure and carries the planted `anomaly`; this is
    the backend-facing *submission* and does not. The W10 runner translates one
    into the other, assembling each transaction's ops from the turns sharing its
    txn_id."""
    subject: str
    predicate: str
    transactions: list[SubmittedTxn] = field(default_factory=list)


@dataclass(slots=True)
class ConcurrentResult:
    """What a store committed for a submitted group (§10.6). The store applies its
    own isolation and reports the outcome; the runner/oracle judges it against the
    permissible-finals set — the store never grades itself.

      final_state  every live memory after the group, across all touched keys,
                   carrying superseded_by (lets the oracle check the supersession
                   chain / lost-update, §10.2).
      reads        per reader transaction, its observed read results in order
                   (lets the oracle check non-repeatable read).
      committed /  conflict resolution under conflict_rule (which writer won; who
      aborted      aborted under abort_both / first-committer-wins)."""
    final_state: list[Memory] = field(default_factory=list)
    reads: dict[TxnId, list[list[Memory]]] = field(default_factory=dict)
    committed: list[TxnId] = field(default_factory=list)
    aborted: list[TxnId] = field(default_factory=list)


# ============================================================================
# Exceptions (§5)
# ============================================================================

class CapabilityNotSupported(Exception):
    """Raised when an operation requires a capability the backend doesn't claim."""

    def __init__(self, capability: Capability, operation: str):
        self.capability = capability
        self.operation = operation
        super().__init__(
            f"operation {operation!r} requires capability "
            f"{capability.value!r}, which this backend does not claim"
        )


class ConformanceViolation(Exception):
    """Raised by the conformance suite when declared capabilities don't match
    observed behavior (B2)."""


# ============================================================================
# The MemoryBackend Protocol (§5) — seven methods, runtime-checkable
# ============================================================================

@runtime_checkable
class MemoryBackend(Protocol):

    def capabilities(self) -> set[Capability]:
        """Stable set of supported capabilities; read once at setup (§6.1)."""
        ...

    def write(self, content: str, options: WriteOptions) -> Any:
        """Persist a new memory, return its opaque ref. Always required.
        If options.signing_key is set, the backend MUST sign per §4.1 and MUST
        claim CRYPTOGRAPHIC_PROVENANCE."""
        ...

    def supersede(self, old_ref: Any, content: str, options: WriteOptions) -> Any:
        """Replace old_ref while preserving history. Requires
        SUPERSESSION_CHAIN, else MUST raise CapabilityNotSupported (§6.3)."""
        ...

    def delete(self, ref: Any) -> bool:
        """Hard-delete. Requires HARD_DELETE. Post-call the ref MUST be
        unrecoverable via retrieve()/audit() (B4). Returns False if not found;
        MUST NOT raise on already-deleted refs (§6.4)."""
        ...

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        """Return memories relevant to query within budget_tokens. Always
        required. as_of without BI_TEMPORAL and tenant_id without MULTI_TENANT
        MUST raise CapabilityNotSupported. Deterministic for fixed state (§6.5)."""
        ...

    def audit(self) -> Iterator[Memory]:
        """Iterate all retrievable memories incl. superseded, EXCLUDING
        hard-deleted. Requires AUDIT (§6.6)."""
        ...

    def flush(self) -> None:
        """Block until preceding mutations are durable + visible. Always
        required; no-op for synchronous in-memory backends (§6.7)."""
        ...


# ============================================================================
# CONCURRENCY_CONTROL extension (§10) — the eighth method, gated
# ============================================================================

@runtime_checkable
class ConcurrentMemoryBackend(MemoryBackend, Protocol):
    """A store claiming CONCURRENCY_CONTROL implements this extension of the
    seven-method core (B3 unchanged). A store that does NOT claim the capability
    does not implement submit_concurrent, and the W10 suite skips it (§10) —
    exactly as a non-MULTI_TENANT store is skipped under W5. The capability flag
    is the operational gate; this Protocol is its type. Such backends declare
    __grafomem_interface__ = INTERFACE_VERSION_V2.

    `declared_policy` is the store's CLAIMED isolation policy — level, conflict
    rule, and coverage guarantee. It self-describes the concurrency claim, the
    parallel of capabilities() for the capability set, so the runner and the W10
    conformance suite can compare achieved behavior against the claim and flag
    over-claims (§10.5). The runner reads it and passes it as `policy` to
    submit_concurrent; the store then realizes some execution admissible under
    it."""

    declared_policy: IsolationPolicy

    def submit_concurrent(self, group: ConcurrentGroup,
                          policy: IsolationPolicy) -> ConcurrentResult:
        """Execute one concurrent group under the declared policy and return the
        committed outcome (§10.6). No real threads — the store realizes some
        admissible serialization deterministically; the runner/oracle checks
        membership in the permissible set. Requires CONCURRENCY_CONTROL."""
        ...


# ============================================================================
# Canonical provenance verification (§4.1, Check P) — Ed25519 over fact_id
# ============================================================================

def verify_provenance(memory: Memory, expected_fact_id: bytes) -> bool:
    """Return True iff `memory` carries a valid Ed25519 signature over
    `expected_fact_id`.

    The harness supplies `expected_fact_id` from ground truth. Because
    MemoryRef is opaque (B5), the doc's `ref_to_fact_id()` self-check is the
    harness's responsibility (it maps retrieved content -> fact_id), so it is
    not re-derived here. A missing signature/public_key while the capability is
    claimed is a Check-P violation — surfaced by the caller, not raised here.

    Requires the optional `cryptography` dependency (grafomem[crypto]); the
    W1 vertical slice does not exercise this path.
    """
    if memory.source is None:
        return False
    if memory.source.signature is None or memory.source.public_key is None:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "verify_provenance requires the 'cryptography' package "
            "(pip install grafomem[crypto])"
        ) from e
    try:
        Ed25519PublicKey.from_public_bytes(memory.source.public_key).verify(
            memory.source.signature, expected_fact_id,
        )
        return True
    except (InvalidSignature, ValueError):
        return False


# ============================================================================
# Smoke check — run `python -m aml.backends.interface`
#
# Defines a throwaway in-memory backend just to prove the Protocol is
# implementable and the capability guards fire. The first *real* baseline is
# the considered persistence floor in persistence.py.
# ============================================================================

if __name__ == "__main__":
    from datetime import timezone

    class _Trivial:
        """Minimal AUDIT-only backend: substring retrieval, no temporal /
        tenant / supersede / delete. Demonstrates the guard pattern."""

        __grafomem_interface__ = INTERFACE_VERSION

        def __init__(self) -> None:
            self._store: dict[int, Memory] = {}
            self._next = 0

        def capabilities(self) -> set[Capability]:
            return {Capability.AUDIT}

        def write(self, content: str, options: WriteOptions) -> int:
            if options.tenant_id is not None:
                raise CapabilityNotSupported(Capability.MULTI_TENANT, "write")
            ref = self._next
            self._next += 1
            self._store[ref] = Memory(
                ref=ref, content=content,
                written_at=datetime.now(tz=timezone.utc),
                metadata=dict(options.metadata),
            )
            return ref

        def supersede(self, old_ref, content, options):
            raise CapabilityNotSupported(Capability.SUPERSESSION_CHAIN, "supersede")

        def delete(self, ref) -> bool:
            raise CapabilityNotSupported(Capability.HARD_DELETE, "delete")

        def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
            if options.as_of is not None:
                raise CapabilityNotSupported(Capability.BI_TEMPORAL, "retrieve")
            if options.tenant_id is not None:
                raise CapabilityNotSupported(Capability.MULTI_TENANT, "retrieve")
            hits = [m for m in self._store.values()
                    if query.lower() in m.content.lower()]
            # Enforce the token-proxy budget (characters), dropping the tail.
            out, used = [], 0
            for m in hits:
                if used + len(m.content) > options.budget_tokens:
                    break
                out.append(m)
                used += len(m.content)
            return out

        def audit(self) -> Iterator[Memory]:
            return iter(list(self._store.values()))

        def flush(self) -> None:
            pass

    print(f"GRAFOMEM interface.py — MemoryBackend Protocol v{INTERFACE_VERSION}\n")

    b = _Trivial()

    # --- Test 1: runtime_checkable Protocol conformance -------------------
    assert isinstance(b, MemoryBackend), "trivial backend does not satisfy Protocol"
    print("✓ Implements MemoryBackend Protocol  (runtime_checkable isinstance)")

    # --- Test 2: 10 independent capability flags (v0.2: +CONCURRENCY_CONTROL)
    assert len(set(Capability)) == 10, f"expected 10 flags, got {len(set(Capability))}"
    assert Capability("audit") is Capability.AUDIT  # StrEnum value round-trip
    assert Capability("concurrency_control") is Capability.CONCURRENCY_CONTROL
    print("✓ Capability enum                    (10 flags, StrEnum values stable)")

    # --- Test 3: write + retrieve round-trips content ---------------------
    r1 = b.write("user lives in Rome", WriteOptions())
    r2 = b.write("user speaks Italian", WriteOptions(metadata={"sess": "s0"}))
    b.flush()
    hits = b.retrieve("rome", RetrieveOptions())
    assert len(hits) == 1 and hits[0].ref == r1
    assert hits[0].metadata == {}
    print("✓ write + retrieve round-trip        (substring hit, ref preserved)")

    # --- Test 4: audit yields everything ----------------------------------
    assert {m.ref for m in b.audit()} == {r1, r2}
    print("✓ audit yields all memories          (2 writes, 2 audited)")

    # --- Test 5: budget_tokens is enforced --------------------------------
    tight = b.retrieve("user", RetrieveOptions(budget_tokens=5))  # nothing fits
    assert tight == [], f"budget not enforced; got {len(tight)}"
    print("✓ retrieve honors budget_tokens      (over-budget tail dropped)")

    # --- Test 6: unclaimed capabilities raise CapabilityNotSupported ------
    for op, call in (
        ("supersede", lambda: b.supersede(r1, "x", WriteOptions())),
        ("delete", lambda: b.delete(r1)),
        ("retrieve as_of", lambda: b.retrieve(
            "x", RetrieveOptions(as_of=datetime.now(tz=timezone.utc)))),
        ("retrieve tenant", lambda: b.retrieve(
            "x", RetrieveOptions(tenant_id="A"))),
        ("write tenant", lambda: b.write("x", WriteOptions(tenant_id="A"))),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Capability guards fire             (supersede/delete/as_of/tenant)")

    # --- Test 7: provenance verifier shape (no crypto dep exercised) ------
    assert verify_provenance(Memory(ref=0, content="x",
                                     written_at=datetime.now(tz=timezone.utc)),
                             b"\x00" * 16) is False
    print("✓ verify_provenance returns False    (no signature -> Check P fails)")

    # --- Test 8: CONCURRENCY_CONTROL extension Protocol (v0.2, §10) --------
    class _ConcurrentToy(_Trivial):
        """AUDIT + CONCURRENCY_CONTROL. Trivial submit_concurrent (no real
        isolation — that is the backend spectrum, not the contract). Proves the
        extension Protocol is implementable and the gate discriminates."""
        declared_policy = IsolationPolicy(
            level=IsolationLevel.READ_COMMITTED,
            conflict_rule=ConflictRule.LAST_COMMITTER_WINS,
            coverage_guarantee=frozenset())
        def capabilities(self) -> set[Capability]:
            return {Capability.AUDIT, Capability.CONCURRENCY_CONTROL}
        def submit_concurrent(self, group: ConcurrentGroup,
                              policy: IsolationPolicy) -> ConcurrentResult:
            return ConcurrentResult(committed=[t.txn_id for t in group.transactions])

    cc = _ConcurrentToy()
    assert isinstance(cc, MemoryBackend)                # base contract intact
    assert isinstance(cc, ConcurrentMemoryBackend)      # and the extension
    assert not isinstance(b, ConcurrentMemoryBackend)   # 7-method backend is not
    assert Capability.CONCURRENCY_CONTROL in cc.capabilities()
    assert isinstance(cc.declared_policy, IsolationPolicy)   # self-describes its claim
    pol = IsolationPolicy(
        level=IsolationLevel.SNAPSHOT,
        conflict_rule=ConflictRule.FIRST_COMMITTER_WINS,
        coverage_guarantee=frozenset({"non_repeatable_read", "lost_update"}),
    )
    grp = ConcurrentGroup(
        subject="user_9", predicate="prefers",
        transactions=[
            SubmittedTxn(txn_id="T1", ops=[TxnOp(OpKind.WRITE, content="tea",
                                                 write_options=WriteOptions())]),
            SubmittedTxn(txn_id="T2", ops=[TxnOp(OpKind.WRITE, content="coffee",
                                                 write_options=WriteOptions())],
                         depends_on=["T1"]),
        ],
    )
    res = cc.submit_concurrent(grp, pol)
    assert isinstance(res, ConcurrentResult) and res.committed == ["T1", "T2"]
    print("✓ CONCURRENCY_CONTROL extension      "
          "(submit_concurrent gated; base contract intact; §10)")

    print("\nAll interface smoke checks green. Contract is implementable; "
          "ready for persistence.py baseline.")
