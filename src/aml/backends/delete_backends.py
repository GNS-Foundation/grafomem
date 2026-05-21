"""
GRAFOMEM delete backends — the deletion spectrum (W6).

Three backends, all claiming {HARD_DELETE, AUDIT} and sharing the same pinned
BGE store. They differ ONLY in what delete() does — which is exactly the point:
the type contract (delete() returns, capability claimed) is identical, but the
*semantic* contract diverges, and only the benchmark's leakage / survivor checks
tell them apart.

  - SoftDeleteBackend   : tombstones the fact but retrieve() still returns it
                          (the soft-delete-without-retrieval-filter bug). audit()
                          reports it gone while retrieve() leaks it — an internal
                          inconsistency. Fails leakage.
  - HonestDeleteBackend : truly removes the fact from store + index. No leak;
                          survivors untouched. The correct one.
  - CoarseDeleteBackend : delete-by-subject — removes EVERY fact sharing the
                          deleted fact's subject (the "delete the entity" pattern
                          done too broadly). No leak, but over-deletes survivors.

All three read structured subject metadata that the harness now passes on write
(WriteOptions.metadata). Requires grafomem[backends].
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


class _DeleteVectorBase:
    """Shared BGE store; subclasses define delete() and the retrieval set."""

    __grafomem_interface__ = "0.1.1"

    def __init__(self, embed_fn: EmbedFn | None = None) -> None:
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._subject: dict[int, str | None] = {}
        self._next = 0

    def _embed(self, texts: list[str]) -> np.ndarray:
        if self._embed_fn is None:
            self._embed_fn = _default_embedder()
        return self._embed_fn(texts)

    def capabilities(self) -> set[Capability]:
        return {Capability.HARD_DELETE, Capability.AUDIT}

    def write(self, content: str, options: WriteOptions) -> int:
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "write")
        if options.signing_key is not None:
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
        self._subject[ref] = options.metadata.get("subject")
        return ref

    def supersede(self, old_ref, content: str, options: WriteOptions) -> int:
        raise CapabilityNotSupported(Capability.SUPERSESSION_CHAIN, "supersede")

    # Subclasses override delete() and, if needed, _retrieval_refs / _audit_refs.
    def delete(self, ref) -> bool:
        raise NotImplementedError

    def _retrieval_refs(self) -> list[int]:
        return list(self._store.keys())

    def _audit_refs(self) -> list[int]:
        return list(self._store.keys())

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        if options.as_of is not None:
            raise CapabilityNotSupported(Capability.BI_TEMPORAL, "retrieve")
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "retrieve")
        refs = self._retrieval_refs()
        if not refs:
            return []
        qv = self._embed([query])[0]
        mat = np.stack([self._vec[r] for r in refs])
        sims = mat @ qv
        order = sorted(range(len(refs)), key=lambda i: (-float(sims[i]), refs[i]))
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
        return iter([self._store[r] for r in self._audit_refs()])

    def flush(self) -> None:
        pass


class SoftDeleteBackend(_DeleteVectorBase):
    """Tombstones on delete, but retrieve() never checks the tombstone -> leaks."""

    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "deletion_policy": "soft tombstone; NOT enforced on retrieve (leaks)",
    }

    def __init__(self, embed_fn: EmbedFn | None = None) -> None:
        super().__init__(embed_fn)
        self._tombstoned: set[int] = set()

    def delete(self, ref) -> bool:
        if ref in self._store:
            self._tombstoned.add(ref)        # claim deletion...
            return True
        return False

    # BUG: retrieval ignores the tombstone set -> deleted content still surfaces.
    def _retrieval_refs(self) -> list[int]:
        return list(self._store.keys())

    # audit reports the fact as gone (the claim) -> audit and retrieve disagree.
    def _audit_refs(self) -> list[int]:
        return [r for r in self._store if r not in self._tombstoned]


class HonestDeleteBackend(_DeleteVectorBase):
    """Truly removes the fact from store + index. No leak; survivors intact."""

    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "deletion_policy": "hard removal from store and index",
    }

    def delete(self, ref) -> bool:
        if ref in self._store:
            del self._store[ref]
            del self._vec[ref]
            self._subject.pop(ref, None)
            return True
        return False


class CoarseDeleteBackend(_DeleteVectorBase):
    """delete-by-subject: removes EVERY fact sharing the target's subject."""

    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "deletion_policy": "delete-by-subject (over-broad; over-deletes survivors)",
    }

    def delete(self, ref) -> bool:
        if ref not in self._store:
            return False
        subj = self._subject.get(ref)
        victims = ([r for r in self._store if self._subject.get(r) == subj]
                   if subj is not None else [ref])
        for r in victims:
            del self._store[r]
            del self._vec[r]
            self._subject.pop(r, None)
        return True


# ============================================================================
# Smoke check — run `python -m aml.backends.delete_backends`
# ============================================================================

if __name__ == "__main__":
    from aml.backends.interface import MemoryBackend
    from aml.backends.vector_only import _stub_embedder

    print("GRAFOMEM delete_backends.py — the deletion spectrum (STUB)\n")

    def fresh(cls):
        b = cls(embed_fn=_stub_embedder())
        # Alice has two facts (a deleted, b survivor); Bob is unrelated.
        a = b.write("Alice lives in Rome", WriteOptions(metadata={"subject": "Alice"}))
        bb = b.write("Alice works at Acme", WriteOptions(metadata={"subject": "Alice"}))
        c = b.write("Bob lives in Paris", WriteOptions(metadata={"subject": "Bob"}))
        return b, a, bb, c

    def has(backend, needle, query):
        return any(needle in m.content
                   for m in backend.retrieve(query, RetrieveOptions(budget_tokens=512)))

    for cls in (SoftDeleteBackend, HonestDeleteBackend, CoarseDeleteBackend):
        b, a, bb, c = fresh(cls)
        assert isinstance(b, MemoryBackend)
        assert b.capabilities() == {Capability.HARD_DELETE, Capability.AUDIT}
    print("✓ All three: Protocol + {HARD_DELETE, AUDIT}")

    # soft: deletes A, but A still leaks on retrieve; audit claims it gone.
    b, a, bb, c = fresh(SoftDeleteBackend)
    assert b.delete(a) is True
    assert has(b, "Alice lives in Rome", "Where does Alice live?"), "soft should LEAK A"
    audited = {m.content for m in b.audit()}
    assert "Alice lives in Rome" not in audited, "soft audit should claim A gone"
    print("✓ soft_delete    LEAKS  (retrieve returns deleted A; audit claims it gone — inconsistent)")

    # honest: A gone everywhere; survivor B and unrelated C intact.
    b, a, bb, c = fresh(HonestDeleteBackend)
    assert b.delete(a) is True
    assert not has(b, "Alice lives in Rome", "Where does Alice live?"), "honest leaked A"
    assert has(b, "Alice works at Acme", "Where does Alice work?"), "honest dropped survivor B"
    assert has(b, "Bob lives in Paris", "Where does Bob live?"), "honest dropped unrelated C"
    print("✓ honest_delete  CLEAN  (A removed; survivor B and unrelated C intact)")

    # coarse: A gone (no leak) but B (same subject) over-deleted; C intact.
    b, a, bb, c = fresh(CoarseDeleteBackend)
    assert b.delete(a) is True
    assert not has(b, "Alice lives in Rome", "Where does Alice live?"), "coarse leaked A"
    assert not has(b, "Alice works at Acme", "Where does Alice work?"), "coarse should over-delete B"
    assert has(b, "Bob lives in Paris", "Where does Bob live?"), "coarse wrongly dropped C"
    print("✓ coarse_delete  OVER-DELETES  (A removed, no leak — but survivor B purged; C intact)")

    # guards
    b, *_ = fresh(HonestDeleteBackend)
    for op, call in (
        ("supersede", lambda: b.supersede(0, "x", WriteOptions())),
        ("as_of", lambda: b.retrieve("x", RetrieveOptions(as_of=datetime.now(tz=timezone.utc)))),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Capability guards (supersede/as_of refused)")

    print("\nAll delete_backends smoke checks green. Next: run_w6 (leakage + over-deletion).")
