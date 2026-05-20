"""
GRAFOMEM backend interface — v0.1.1.

Implements 02-backend-interface.md: the `MemoryBackend` Protocol that every
architecture-under-test implements, the capability-flag system, the core
types, the exception classes, and the canonical Ed25519 provenance verifier.

Design anchors (doc 02 §2):
  B1  capabilities are declared, not inferred — the harness adapts to the
      declaration and never penalizes honest omissions.
  B3  the interface is minimal but sufficient — seven methods, no more.
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

INTERFACE_VERSION = "0.1.1"


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

    # --- Test 2: 9 independent capability flags ---------------------------
    assert len(set(Capability)) == 9, f"expected 9 flags, got {len(set(Capability))}"
    assert Capability("audit") is Capability.AUDIT  # StrEnum value round-trip
    print("✓ Capability enum                    (9 flags, StrEnum values stable)")

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

    print("\nAll interface smoke checks green. Contract is implementable; "
          "ready for persistence.py baseline.")
