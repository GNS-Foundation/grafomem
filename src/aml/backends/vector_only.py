"""
GRAFOMEM vector_only reference adapter (doc 02 §9.1).

Verbatim store + semantic retrieval: every write is embedded; retrieve()
embeds the query and returns the highest-cosine memories within budget_tokens.
Unlike the persistence floor, retrieval is query-dependent — it can surface a
fact the recency window dropped, which is exactly where it should beat the
floor on long-horizon recall.

Reference-adapter discipline:
  - Embedding model is PINNED to BGE-small-en-v1.5 (a controlled variable
    across findings). Non-reference adapters declare their own model in
    __grafomem_adapter_metadata__; this one pins the reference.
  - Index is EXACT brute-force cosine (numpy), not ANN. At W1 scale (<=500
    facts) brute-force is trivially fast, and—more importantly—introduces no
    approximation noise, so the embedding stays the only variable. FAISS/Qdrant
    can slot in behind the same interface when scale demands it.

Embeddings are L2-normalized, so a dot product is the cosine similarity.
Claims {AUDIT}.

Requires grafomem[backends] (sentence-transformers + numpy). The embedder is
injectable: pass embed_fn for tests/controlled reruns; leave it None to lazily
load the pinned reference model on first use.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import numpy as np

from aml.backends.interface import (
    Capability,
    CapabilityNotSupported,
    Memory,
    RetrieveOptions,
    WriteOptions,
)

REFERENCE_MODEL = "BAAI/bge-small-en-v1.5"
__grafomem_interface__ = "0.1.1"

# texts -> (n, d) L2-normalized float32 array
EmbedFn = Callable[[list[str]], np.ndarray]


def _default_embedder() -> EmbedFn:
    """Lazily build the pinned BGE-small embedder (downloads ~130MB once)."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "vector_only needs grafomem[backends] "
            "(pip install -e \".[backends]\")"
        ) from e
    model = SentenceTransformer(REFERENCE_MODEL)

    def embed(texts: list[str]) -> np.ndarray:
        return np.asarray(
            model.encode(texts, normalize_embeddings=True,
                         convert_to_numpy=True),
            dtype=np.float32,
        )

    return embed


class VectorOnlyBackend:
    """BGE-small + exact cosine. The first real backend; the floor's foil."""

    __grafomem_interface__ = "0.1.1"
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference",
        "embedding_model": REFERENCE_MODEL,
        "vector_store": "numpy-bruteforce-cosine-exact",
        "notes": "Reference baseline. Exact cosine, no ANN. L2-normalized embeddings.",
    }

    def __init__(self, embed_fn: EmbedFn | None = None) -> None:
        self._embed_fn = embed_fn               # None -> lazy default model
        self._store: dict[int, Memory] = {}
        self._vecs: list[tuple[int, np.ndarray]] = []
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
            raise CapabilityNotSupported(
                Capability.CRYPTOGRAPHIC_PROVENANCE, "write")
        ref = self._next
        self._next += 1
        self._store[ref] = Memory(
            ref=ref, content=content,
            written_at=datetime.now(tz=timezone.utc),
            metadata=dict(options.metadata),
        )
        self._vecs.append((ref, self._embed([content])[0]))
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
        if not self._vecs:
            return []
        qv = self._embed([query])[0]                       # (d,), normalized
        refs = [r for r, _ in self._vecs]
        mat = np.stack([v for _, v in self._vecs])         # (n, d)
        sims = mat @ qv                                    # cosine (normalized)
        sims = np.nan_to_num(sims, nan=-1.0)               # fallback for degenerate zero vectors
        # Deterministic order: descending similarity, ties broken by ref (§6.5).
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
# Smoke check — run `python -m aml.backends.vector_only`
#
# Uses a DETERMINISTIC STUB embedder (hashed bag-of-words -> cosine) so the
# adapter logic + harness integration are verifiable WITHOUT downloading the
# real model. The stub ranks by lexical overlap; the real BGE-small ranks
# semantically. The mechanics are identical — only the vectors differ.
# ============================================================================

def _stub_embedder(dim: int = 256) -> EmbedFn:
    import hashlib
    import re
    tok = re.compile(r"[a-z0-9]+")

    def embed(texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in tok.findall(t.lower()):
                h = int.from_bytes(
                    hashlib.blake2b(w.encode(), digest_size=4).digest(), "big"
                ) % dim
                out[i, h] += 1.0
            n = float(np.linalg.norm(out[i]))
            if n > 0:
                out[i] /= n
        return out

    return embed


if __name__ == "__main__":
    from aml.backends.interface import MemoryBackend

    print("GRAFOMEM vector_only.py — BGE-small + exact cosine "
          "(STUB embedder for smoke)\n")

    b = VectorOnlyBackend(embed_fn=_stub_embedder())

    assert isinstance(b, MemoryBackend)
    print("✓ Implements MemoryBackend Protocol")
    assert b.capabilities() == {Capability.AUDIT}
    assert b.__grafomem_adapter_metadata__["embedding_model"] == REFERENCE_MODEL
    print("✓ Claims {AUDIT}; pins BGE-small      (adapter metadata declared)")

    # Write distinct facts; the query must retrieve the relevant one, not the
    # most recent — the whole difference from the floor.
    b.write("Alice lives in Rome", WriteOptions())
    b.write("Bob speaks Italian", WriteOptions())
    b.write("Carol plays the violin", WriteOptions())        # most recent
    b.flush()
    hits = b.retrieve("Where does Alice live?", RetrieveOptions(budget_tokens=64))
    assert hits and hits[0].content == "Alice lives in Rome", \
        f"semantic-ish retrieval failed; got {[h.content for h in hits]}"
    print("✓ Query-dependent retrieval          (finds Alice, not the newest)")

    # Determinism (§6.5): same state + query -> same list.
    again = b.retrieve("Where does Alice live?", RetrieveOptions(budget_tokens=64))
    assert [h.ref for h in again] == [h.ref for h in hits]
    print("✓ Deterministic retrieval            (stable order, tie-break by ref)")

    # Budget + guards.
    assert b.retrieve("Alice", RetrieveOptions(budget_tokens=5)) == []
    for op, call in (
        ("as_of", lambda: b.retrieve("x", RetrieveOptions(
            as_of=datetime.now(tz=timezone.utc)))),
        ("tenant", lambda: b.retrieve("x", RetrieveOptions(tenant_id="A"))),
        ("supersede", lambda: b.supersede(0, "x", WriteOptions())),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Budget + capability guards          (over-budget empty; as_of/tenant/supersede raise)")

    print("\nAll vector_only smoke checks green (STUB). Real BGE-small numbers "
          "come from scripts/run_w1.py on your machine.")
