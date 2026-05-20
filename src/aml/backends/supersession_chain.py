"""
GRAFOMEM supersession_chain reference adapter.

Same pinned BGE-small vector store as vector_only, but it actually implements
supersede(): the old version is RETIRED from the retrieval candidate set, so
retrieve() ranks only current (non-superseded) heads. audit() still yields the
superseded versions (per doc 02 §6.6) with their superseded_by link set.

Why this matters on W2 (Drift): vector_only keeps every version, so a "current"
query sees d semantically-identical candidates per chain and picks the right
(head) one only ~1/d of the time at a tight budget. By retiring superseded
versions, this backend leaves exactly ONE current candidate per (subject,
predicate) — the head — turning the drift problem back into an unambiguous
W1-style match. The expected result: tight-budget current-query recall recovers
from ~1/depth toward ~1.0. That gain comes from a CAPABILITY, not a better
embedding (the embedder is identical to vector_only's).

Claims {SUPERSESSION_CHAIN, AUDIT}. Not BI_TEMPORAL: it tracks "what is current"
but discards the historical view, so as_of queries remain N/A (a bi_temporal
adapter is the next rung). Requires grafomem[backends].
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


class SupersessionVectorBackend:
    """BGE vector store that retires superseded versions on supersede()."""

    __grafomem_interface__ = "0.1.1"
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference",
        "embedding_model": REFERENCE_MODEL,
        "vector_store": "numpy-bruteforce-cosine-exact",
        "supersession_policy": "retire-old-from-retrieval; keep in audit",
        "notes": "Same embedder as vector_only; difference is the capability, not the model.",
    }

    def __init__(self, embed_fn: EmbedFn | None = None) -> None:
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._superseded: set[int] = set()       # retired from retrieval
        self._next = 0

    def _embed(self, texts: list[str]) -> np.ndarray:
        if self._embed_fn is None:
            self._embed_fn = _default_embedder()
        return self._embed_fn(texts)

    def capabilities(self) -> set[Capability]:
        return {Capability.SUPERSESSION_CHAIN, Capability.AUDIT}

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
        return ref

    def supersede(self, old_ref, content: str, options: WriteOptions) -> int:
        # §6.3: if old_ref is unknown, behave as a fresh write.
        new_ref = self.write(content, options)
        if old_ref in self._store:
            self._superseded.add(old_ref)
            self._store[old_ref].superseded_by = new_ref   # for audit/conformance
        return new_ref

    def delete(self, ref) -> bool:
        raise CapabilityNotSupported(Capability.HARD_DELETE, "delete")

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        if options.as_of is not None:
            raise CapabilityNotSupported(Capability.BI_TEMPORAL, "retrieve")
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "retrieve")
        # Candidates = current (non-superseded) memories only.
        refs = [r for r in self._store if r not in self._superseded]
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
        # AUDIT: all memories incl. superseded, excluding deleted (none here).
        return iter(list(self._store.values()))

    def flush(self) -> None:
        pass


# ============================================================================
# Smoke check — run `python -m aml.backends.supersession_chain`
# ============================================================================

if __name__ == "__main__":
    from aml.backends.interface import MemoryBackend
    from aml.backends.vector_only import _stub_embedder

    print("GRAFOMEM supersession_chain.py — retire-on-supersede (STUB embedder)\n")

    b = SupersessionVectorBackend(embed_fn=_stub_embedder())

    assert isinstance(b, MemoryBackend)
    assert b.capabilities() == {Capability.SUPERSESSION_CHAIN, Capability.AUDIT}
    print("✓ Protocol + capabilities            ({SUPERSESSION_CHAIN, AUDIT})")

    # Build a drift chain: Aria lived in Rome, then Milan, then Turin.
    r0 = b.write("Aria lives in Rome", WriteOptions())
    r1 = b.supersede(r0, "Aria lives in Milan", WriteOptions())
    r2 = b.supersede(r1, "Aria lives in Turin", WriteOptions())
    b.flush()

    # A current query must return ONLY the head (Turin); stale versions retired.
    hits = b.retrieve("Where does Aria live?", RetrieveOptions(budget_tokens=512))
    contents = [m.content for m in hits]
    assert "Aria lives in Turin" in contents, f"head missing: {contents}"
    assert "Aria lives in Rome" not in contents, f"stale Rome leaked: {contents}"
    assert "Aria lives in Milan" not in contents, f"stale Milan leaked: {contents}"
    print("✓ retrieve() returns only the head   (Rome/Milan retired; Turin current)")

    # audit() still exposes the full history, with superseded_by linked.
    audited = {m.content: m for m in b.audit()}
    assert len(audited) == 3, f"audit should yield all 3, got {len(audited)}"
    assert audited["Aria lives in Rome"].superseded_by == r1
    assert audited["Aria lives in Milan"].superseded_by == r2
    assert audited["Aria lives in Turin"].superseded_by is None
    print("✓ audit() yields full history        (3 versions, superseded_by linked)")

    # Tight budget still finds the head — one current candidate, not three.
    tight = b.retrieve("Where does Aria live?", RetrieveOptions(budget_tokens=20))
    assert tight and tight[0].content == "Aria lives in Turin"
    print("✓ Tight budget finds head            (no version confusion -> W1-like)")

    # Capability guards: as_of (no BI_TEMPORAL) and tenant raise.
    for op, call in (
        ("as_of", lambda: b.retrieve("x", RetrieveOptions(
            as_of=datetime.now(tz=timezone.utc)))),
        ("delete", lambda: b.delete(r0)),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Capability guards                  (as_of -> no BI_TEMPORAL; delete -> no HARD_DELETE)")

    print("\nAll supersession_chain smoke checks green. Run scripts/run_w2.py "
          "for the three-way W2 comparison.")
