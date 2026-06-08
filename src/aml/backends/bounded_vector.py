"""
GRAFOMEM bounded_vector reference adapter.

Same pinned BGE-small vector store as vector_only, but with a fixed capacity:
on write, once the store exceeds `capacity`, the OLDEST memory is evicted
(FIFO — a recency window). This is the realistic "context window" model an agent
lives inside: bounded footprint and bounded per-query scan cost, at the price of
forgetting anything older than the window.

On W4 (Long-Horizon Dependencies) this produces the recall-vs-footprint
tradeoff: a query for a fact d facts back succeeds only if d < capacity (else it
was evicted), so recall cliffs at d = capacity — a structural fact, independent
of the embedder — while footprint and scan cost plateau at `capacity` instead of
growing linearly with the horizon like an unbounded store.

Claims {AUDIT}: no temporal, supersession, or hard-delete semantics. Eviction is
capacity management, not a HARD_DELETE (the caller did not request removal), and
is reflected in audit() — which exposes exactly the retained window.
Requires grafomem[backends].
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from datetime import datetime, timezone

import numpy as np

from aml.backends.interface import (
    Capability,
    CapabilityNotSupported,
    Memory,
    RetrieveOptions,
    WriteOptions,
)
from aml.backends.vector_only import REFERENCE_MODEL, EmbedFn, _default_embedder

__grafomem_interface__ = "0.1.1"

DEFAULT_CAPACITY = 64


class BoundedVectorBackend:
    """BGE vector store with a fixed-capacity recency window (FIFO eviction)."""

    __grafomem_interface__ = "0.1.1"

    def __init__(self, capacity: int = DEFAULT_CAPACITY,
                 embed_fn: EmbedFn | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._order: deque[int] = deque()      # FIFO eviction order
        self._next = 0
        self._evicted = 0                       # bookkeeping (not retrievable)

    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference",
        "embedding_model": REFERENCE_MODEL,
        "vector_store": "numpy-bruteforce-cosine-exact",
        "retention_policy": "fixed-capacity recency window, FIFO eviction",
    }

    def _embed(self, texts: list[str]) -> np.ndarray:
        if self._embed_fn is None:
            self._embed_fn = _default_embedder()
        return self._embed_fn(texts)

    def capabilities(self) -> set[Capability]:
        return {Capability.AUDIT}

    def write(self, content: str, options: WriteOptions) -> int:
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "write")
        if options.signing_identity is not None:
            raise CapabilityNotSupported(
                Capability.CRYPTOGRAPHIC_PROVENANCE, "write")
        ref = self._next
        self._next += 1
        self._store[ref] = Memory(
            ref=ref, content=content,
            written_at=datetime.now(tz=timezone.utc),
            metadata=dict(options.metadata),
        )
        self._vec[ref] = self._embed([content])[0]
        self._order.append(ref)
        while len(self._order) > self.capacity:        # evict oldest
            old = self._order.popleft()
            del self._store[old]
            del self._vec[old]
            self._evicted += 1
        return ref

    def supersede(self, old_ref, content: str, options: WriteOptions) -> int:
        raise CapabilityNotSupported(Capability.SUPERSESSION_CHAIN, "supersede")

    def delete(self, ref) -> bool:
        raise CapabilityNotSupported(Capability.HARD_DELETE, "delete")

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        if options.as_of is not None:
            raise CapabilityNotSupported(Capability.BI_TEMPORAL, "retrieve")
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "retrieve")
        refs = list(self._store.keys())               # retained window only
        if not refs:
            return []
        qv = self._embed([query])[0]
        mat = np.stack([self._vec[r] for r in refs])
        sims = mat @ qv
        order = sorted(range(len(refs)),
                       key=lambda i: (-float(sims[i]), refs[i]))
        out: list[Memory] = []
        used = 0
        for i in order:
            m = self._store[refs[i]]
            cost = len(m.content)
            if used + cost > options.budget_tokens:
                break
            out.append(m)
            used += cost
        return out

    def audit(self) -> Iterator[Memory]:
        return iter(list(self._store.values()))        # the retained window

    def flush(self) -> None:
        pass


# ============================================================================
# Smoke check — run `python -m aml.backends.bounded_vector`
# ============================================================================

if __name__ == "__main__":
    from aml.backends.interface import MemoryBackend
    from aml.backends.vector_only import _stub_embedder

    print("GRAFOMEM bounded_vector.py — fixed-capacity recency window (STUB)\n")

    K = 10
    b = BoundedVectorBackend(capacity=K, embed_fn=_stub_embedder())

    assert isinstance(b, MemoryBackend)
    assert b.capabilities() == {Capability.AUDIT}
    print(f"✓ Protocol + capabilities            ({{AUDIT}}, capacity={K})")

    # Write 100 facts; only the last K survive.
    refs = [b.write(f"Entity{i:03d} lives in City{i:03d}", WriteOptions())
            for i in range(100)]
    b.flush()
    retained = {m.ref for m in b.audit()}
    assert len(retained) == K, f"expected {K} retained, got {len(retained)}"
    assert retained == set(refs[-K:]), "retained window is not the last K (FIFO)"
    print(f"✓ FIFO eviction                      (100 written, last {K} retained)")

    # A recent fact is retrievable; an evicted (old) one is not.
    recent = b.retrieve("Where does Entity099 live?", RetrieveOptions(budget_tokens=512))
    assert any("Entity099" in m.content for m in recent), "recent fact missing"
    old = b.retrieve("Where does Entity001 live?", RetrieveOptions(budget_tokens=512))
    assert not any("Entity001" in m.content for m in old), "evicted fact leaked"
    print("✓ Recall cliff                       (recent retrievable; evicted absent)")

    # Footprint = retained count = scan cost, plateaued at capacity.
    assert len(list(b.audit())) == K
    print(f"✓ Footprint plateau                  (audit/scan = {K}, not 100)")

    # Capability guards.
    for op, call in (
        ("supersede", lambda: b.supersede(refs[-1], "x", WriteOptions())),
        ("delete", lambda: b.delete(refs[-1])),
        ("as_of", lambda: b.retrieve("x", RetrieveOptions(
            as_of=datetime.now(tz=timezone.utc)))),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Capability guards                  (supersede/delete/as_of refused)")

    # Determinism: same writes -> same retained set + ranking.
    b2 = BoundedVectorBackend(capacity=K, embed_fn=_stub_embedder())
    for i in range(100):
        b2.write(f"Entity{i:03d} lives in City{i:03d}", WriteOptions())
    r1 = [m.ref for m in b.retrieve("Where does Entity095 live?", RetrieveOptions(budget_tokens=512))]
    r2 = [m.ref for m in b2.retrieve("Where does Entity095 live?", RetrieveOptions(budget_tokens=512))]
    assert r1 == r2
    print("✓ Deterministic                      (same writes -> same window + ranking)")

    print("\nAll bounded_vector smoke checks green. Next: run_w4 "
          "(recall-by-distance + footprint vs unbounded).")
