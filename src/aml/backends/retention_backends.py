"""
GRAFOMEM retention backends — the importance-weighted store (W8).

W4's bounded_vector evicts by recency (FIFO): at capacity, the OLDEST fact goes,
regardless of worth. That is the right policy only when every fact is equally
important — which is exactly W4's world. W8 introduces a world where some facts
matter and most do not, and asks whether a smarter eviction policy holds recall
at the same footprint.

ImportanceBoundedBackend is that smarter policy: same fixed capacity K, same
{AUDIT} contract, same exact-cosine retrieval as bounded_vector — but at
capacity it evicts the LOWEST-importance fact first, breaking ties by age (FIFO
within an importance class). It reads each fact's importance from
WriteOptions.metadata['importance'] (run_w8 supplies it; absent -> 0.0, i.e.
treated as maximally evictable). Because W8 guarantees the high-importance facts
number <= K, this store keeps EVERY high fact at every distance while FIFO evicts
the far ones — the forgetting-curve contrast, at identical footprint.

Retention is not deletion (gmp-spec §5.4): eviction is capacity management, the
caller never asked for removal, and audit() exposes exactly the retained window.
So this backend claims only {AUDIT} and adds NO capability flag — it differs from
bounded_vector solely by its declared retention policy. Requires
grafomem[backends] for the real BGE; the smoke uses the stub embedder.
"""

from __future__ import annotations

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


class ImportanceBoundedBackend:
    """BGE vector store with fixed capacity that evicts lowest-importance-first.

    Eviction key is (importance, ref): the minimum is the least important fact,
    and among equally (un)important facts the oldest (smallest ref = earliest
    write). A brand-new fact strictly less important than everything retained is
    evicted immediately — it never displaces a more important one.
    """

    __grafomem_interface__ = "0.1.1"
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference",
        "embedding_model": REFERENCE_MODEL,
        "vector_store": "numpy-bruteforce-cosine-exact",
        "retention_policy": "fixed-capacity, importance-weighted eviction "
                            "(lowest-importance-first, FIFO tie-break)",
    }

    def __init__(self, capacity: int = DEFAULT_CAPACITY,
                 embed_fn: EmbedFn | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._imp: dict[int, float] = {}
        self._next = 0
        self._evicted = 0

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
        self._imp[ref] = float(options.metadata.get("importance", 0.0))
        while len(self._store) > self.capacity:        # evict lowest-importance
            victim = min(self._store, key=lambda r: (self._imp[r], r))
            del self._store[victim]
            del self._vec[victim]
            del self._imp[victim]
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
        refs = list(self._store.keys())                # retained window only
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
        return iter(list(self._store.values()))         # the retained window

    def flush(self) -> None:
        pass


class SummarisingRetentionBackend:
    """BGE vector store that compacts the lowest-importance facts when capacity is exceeded.
    
    Instead of dropping facts silently, it merges the 2 lowest-importance facts into a single 
    structural summary memory, mapped to their original fact_ids in metadata['compacts'].
    Lossless in string length, but structurally lossy because the summary consumes
    more budget and dilutes in the embedder.
    """

    __grafomem_interface__ = "0.1.1"
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference",
        "embedding_model": REFERENCE_MODEL,
        "vector_store": "numpy-bruteforce-cosine-exact",
        "retention_policy": "fixed-capacity, summarise/merge compaction",
    }

    def __init__(self, capacity: int = DEFAULT_CAPACITY,
                 embed_fn: EmbedFn | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._imp: dict[int, float] = {}
        self._next = 0

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
            raise CapabilityNotSupported(Capability.CRYPTOGRAPHIC_PROVENANCE, "write")
        
        ref = self._next
        self._next += 1
        
        # Determine fact_id if we have subject/predicate (from harness/w8 injection)
        compacts = []
        if "subject" in options.metadata and "predicate" in options.metadata:
            # We don't have the real fact_id generator here easily, but W8 runner 
            # uses the write_id directly. We can just store the 'ref' logically, or 
            # W8 relies on retrieved refs. Wait, W8 checks ref_to_fids[m.ref].
            pass

        self._store[ref] = Memory(
            ref=ref, content=content,
            written_at=datetime.now(tz=timezone.utc),
            metadata=dict(options.metadata),
        )
        self._vec[ref] = self._embed([content])[0]
        self._imp[ref] = float(options.metadata.get("importance", 0.0))
        
        # Compaction loop
        while len(self._store) > self.capacity:
            # Pick the 2 lowest-importance facts
            ordered = sorted(self._store.keys(), key=lambda r: (self._imp[r], r))
            v1, v2 = ordered[0], ordered[1]
            
            m1, m2 = self._store[v1], self._store[v2]
            
            # structural merge:
            new_content = m1.content + " | " + m2.content
            
            # merge compacts lists or create from refs. 
            # run_w8 builds a map `ref_to_fids[ref]`. If we just create a new ref, 
            # run_w8 won't know it maps to old fids. 
            # BUT the spec says: "metadata['compacts'] stores the list of original fact_ids"
            # wait, if run_w8 injects original fact_id into metadata... let's check run_w8.py.
            # Currently run_w8 does not inject fact_id into metadata! 
            # "metadata=({"subject": f.subject, "predicate": f.predicate, "importance": f.importance} if f else {})"
            # We must update run_w8 to inject "fact_id", or have this backend just track original refs.
            # We'll use metadata['compacts'] to store original refs from THIS store,
            # and let run_w8 look at metadata['compacts'] to resolve original fact_ids!
            c1 = m1.metadata.get("compacts", [v1])
            c2 = m2.metadata.get("compacts", [v2])
            new_compacts = c1 + c2
            
            # The new summary gets the max importance of the two (or min? Min ensures it keeps getting compacted)
            # Let's use max so high importance facts don't get dragged down, 
            # but wait, we only compact the 2 LOWEST. So they'll both be 0.1.
            new_imp = max(self._imp[v1], self._imp[v2])
            
            new_ref = self._next
            self._next += 1
            
            self._store[new_ref] = Memory(
                ref=new_ref, content=new_content,
                written_at=datetime.now(tz=timezone.utc),
                metadata={"importance": new_imp, "compacts": new_compacts},
            )
            self._vec[new_ref] = self._embed([new_content])[0]
            self._imp[new_ref] = new_imp
            
            del self._store[v1]
            del self._vec[v1]
            del self._imp[v1]
            del self._store[v2]
            del self._vec[v2]
            del self._imp[v2]

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
        refs = list(self._store.keys())
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
        return iter(list(self._store.values()))

    def flush(self) -> None:
        pass


# ============================================================================
# Smoke check — run `python -m aml.backends.retention_backends`
# ============================================================================

if __name__ == "__main__":
    from aml.backends.interface import MemoryBackend
    from aml.backends.vector_only import _stub_embedder

    print("GRAFOMEM retention_backends.py — importance-weighted store (STUB)\n")

    K = 10
    b = ImportanceBoundedBackend(capacity=K, embed_fn=_stub_embedder())
    assert isinstance(b, MemoryBackend)
    assert b.capabilities() == {Capability.AUDIT}
    print(f"✓ Protocol + capabilities  ({{AUDIT}}, capacity={K}, no new flag)")

    def W(imp):
        return WriteOptions(metadata={"importance": imp})

    # Write 3 high-importance facts FIRST (old), then a torrent of 100 low ones.
    high = [b.write(f"VIP{i} lives in Capital{i}", W(1.0)) for i in range(3)]
    for i in range(100):
        b.write(f"filler{i} lives in town{i}", W(0.1))
    b.flush()
    retained = {m.ref for m in b.audit()}
    # All 3 old high-importance facts survive despite being the OLDEST writes...
    assert set(high) <= retained, "importance store evicted a high fact (should keep)"
    # ...and the window is exactly K (footprint plateaus, like FIFO).
    assert len(retained) == K, f"footprint should plateau at K={K}, got {len(retained)}"
    print(f"✓ Keeps the important     (3 oldest high facts retained through 100 low writes)")
    print(f"✓ Footprint plateau       (retained = {len(retained)} = K, not 103)")

    # The high facts are retrievable; the oldest low filler is gone.
    assert any("VIP0" in m.content
               for m in b.retrieve("Where does VIP0 live?", RetrieveOptions(budget_tokens=1 << 20))), \
        "high fact not retrievable"
    assert not any("filler0" in m.content
                   for m in b.retrieve("Where does filler0 live?", RetrieveOptions(budget_tokens=1 << 20))), \
        "oldest low filler should have been evicted"
    print("✓ Eviction is by worth     (old high kept; old low evicted)")

    # Contrast with FIFO at the same K: FIFO drops the old high facts.
    from aml.backends.bounded_vector import BoundedVectorBackend
    f = BoundedVectorBackend(capacity=K, embed_fn=_stub_embedder())
    for r in range(3):
        f.write(f"VIP{r} lives in Capital{r}", W(1.0))
    for i in range(100):
        f.write(f"filler{i} lives in town{i}", W(0.1))
    f.flush()
    assert not any("VIP0" in m.content
                   for m in f.retrieve("Where does VIP0 live?", RetrieveOptions(budget_tokens=1 << 20))), \
        "FIFO should have evicted the old high fact"
    print("✓ FIFO contrast            (same K, FIFO evicted the old high fact — the W8 split)")

    # Capability guards.
    for op, call in (
        ("supersede", lambda: b.supersede(high[-1], "x", WriteOptions())),
        ("delete", lambda: b.delete(high[-1])),
        ("as_of", lambda: b.retrieve("x", RetrieveOptions(as_of=datetime.now(tz=timezone.utc)))),
        ("tenant", lambda: b.retrieve("x", RetrieveOptions(tenant_id="A"))),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Capability guards        (supersede/delete/as_of/tenant refused)")

    print("\nAll retention_backends smoke checks green. Next: run_w8 "
          "(recall-by-distance + footprint vs FIFO and unbounded).")
