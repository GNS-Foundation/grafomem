"""
GRAFOMEM bi_temporal reference adapter.

Same pinned BGE-small vector store as vector_only / supersession_chain, but it
keeps a valid-time interval [valid_from, valid_until) per version and resolves
retrieve(as_of=t) to the version valid at t. This is a strict superset of
supersession_chain:

  - as_of = None  -> current query: candidates are open-interval heads
                     (valid_until is None) -> identical to supersession_chain.
  - as_of = t     -> historical query: candidates are versions whose interval
                     contains t (valid_from <= t < valid_until) -> one version
                     per chain, the slice that was valid then.

It learns each interval the way the trace defines it (w2.py): a version's
valid_from arrives on write/supersede via WriteOptions; its valid_until is the
NEXT version's valid_from, which the harness hands to supersede() as the new
version's valid_from. So supersede(old, new) closes old at opts.valid_from and
reconstructs the oracle's contiguous windows exactly, with no extra plumbing.

Why it matters: historical (as_of) queries are N/A for every non-BI_TEMPORAL
backend (the harness skips them, E1). This adapter is the only one that can
answer them, so on W2 those queries flip from unscored to scored — the second
capability axis. Claims {BI_TEMPORAL, SUPERSESSION_CHAIN, AUDIT}; it must claim
SUPERSESSION_CHAIN so the harness dispatches supersede() and the intervals get
closed. Requires grafomem[backends].
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


class BiTemporalVectorBackend:
    """BGE vector store with per-version valid-time intervals + as_of resolution."""

    __grafomem_interface__ = "0.1.1"
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference",
        "embedding_model": REFERENCE_MODEL,
        "vector_store": "numpy-bruteforce-cosine-exact",
        "temporal_policy": "valid-time intervals [valid_from, valid_until); "
                           "as_of resolves to the version valid at t",
        "notes": "Same embedder as vector_only/supersession_chain; the "
                 "difference is the BI_TEMPORAL capability, not the model.",
    }

    def __init__(self, embed_fn: EmbedFn | None = None) -> None:
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._vfrom: dict[int, datetime | None] = {}
        self._vuntil: dict[int, datetime | None] = {}   # None = open (head)
        self._next = 0

    def _embed(self, texts: list[str]) -> np.ndarray:
        if self._embed_fn is None:
            self._embed_fn = _default_embedder()
        return self._embed_fn(texts)

    def capabilities(self) -> set[Capability]:
        return {Capability.BI_TEMPORAL, Capability.SUPERSESSION_CHAIN,
                Capability.AUDIT}

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
        self._vfrom[ref] = options.valid_from   # None for non-temporal workloads
        self._vuntil[ref] = None                 # open until superseded
        return ref

    def supersede(self, old_ref, content: str, options: WriteOptions) -> int:
        # New version's valid_from == predecessor's valid_until (contiguous, w2.py).
        new_ref = self.write(content, options)
        if old_ref in self._store:
            self._vuntil[old_ref] = options.valid_from
            self._store[old_ref].superseded_by = new_ref
        return new_ref

    def delete(self, ref) -> bool:
        # No deletes in W1-W6; a bi-temporal tombstone would close the interval
        # with no successor, but no workload exercises it, so we don't claim it.
        raise CapabilityNotSupported(Capability.HARD_DELETE, "delete")

    def _valid_at(self, ref: int, t: datetime | None) -> bool:
        if t is None:                            # current: open intervals only
            return self._vuntil[ref] is None
        vf, vu = self._vfrom[ref], self._vuntil[ref]
        return (vf is None or vf <= t) and (vu is None or t < vu)

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "retrieve")
        t = options.as_of
        refs = [r for r in self._store if self._valid_at(r, t)]
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
# Smoke check — run `python -m aml.backends.bi_temporal`
# ============================================================================

if __name__ == "__main__":
    from datetime import timedelta

    from aml.backends.interface import MemoryBackend
    from aml.backends.vector_only import VectorOnlyBackend, _stub_embedder
    from aml.eval.harness import run_trace
    from aml.generator.trace import Difficulty
    from aml.generator.workloads.w2 import generate_w2

    print("GRAFOMEM bi_temporal.py — valid-time as_of resolution (STUB embedder)\n")

    b = BiTemporalVectorBackend(embed_fn=_stub_embedder())

    assert isinstance(b, MemoryBackend)
    assert b.capabilities() == {Capability.BI_TEMPORAL,
                                Capability.SUPERSESSION_CHAIN, Capability.AUDIT}
    print("✓ Protocol + capabilities            ({BI_TEMPORAL, SUPERSESSION_CHAIN, AUDIT})")

    # Hand-built chain with explicit contiguous windows:
    #   Rome  [t0, t1)   Milan [t1, t2)   Turin [t2, open)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=30)
    t2 = t0 + timedelta(days=60)
    r0 = b.write("Aria lives in Rome", WriteOptions(valid_from=t0))
    r1 = b.supersede(r0, "Aria lives in Milan", WriteOptions(valid_from=t1))
    r2 = b.supersede(r1, "Aria lives in Turin", WriteOptions(valid_from=t2))
    b.flush()

    q = "Where does Aria live?"
    now = [m.content for m in b.retrieve(q, RetrieveOptions(as_of=None))]
    assert now == ["Aria lives in Turin"], now
    print("✓ as_of=None -> head                 (Turin)")

    # Time-travel: midpoint of each window resolves to that version.
    for label, t, expected in (
        ("inside Rome window ", t0 + timedelta(days=15), "Aria lives in Rome"),
        ("inside Milan window", t1 + timedelta(days=15), "Aria lives in Milan"),
        ("inside Turin window", t2 + timedelta(days=15), "Aria lives in Turin"),
    ):
        got = [m.content for m in b.retrieve(q, RetrieveOptions(as_of=t))]
        assert got == [expected], f"{label}: {got}"
    print("✓ as_of=t -> version valid at t      (Rome / Milan / Turin by window)")

    audited = {m.content: m for m in b.audit()}
    assert len(audited) == 3 and audited["Aria lives in Milan"].superseded_by == r2
    print("✓ audit() yields full history        (3 versions, superseded_by linked)")

    for op, call in (
        ("tenant", lambda: b.retrieve("x", RetrieveOptions(tenant_id="t"))),
        ("delete", lambda: b.delete(r0)),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Capability guards                  (tenant -> no MULTI_TENANT; delete -> no HARD_DELETE)")

    # Reclamation on a real W2 hard trace, through the actual harness:
    # bi_temporal answers every query (n_na=0); vector_only skips all historical.
    tr = generate_w2(seed=0, difficulty=Difficulty.HARD)
    bt_run = run_trace(BiTemporalVectorBackend(embed_fn=_stub_embedder()),
                       tr, budget_tokens=512)
    vec_run = run_trace(VectorOnlyBackend(embed_fn=_stub_embedder()),
                        tr, budget_tokens=512)
    assert bt_run.n_na == 0, f"bi_temporal should answer all queries, n_na={bt_run.n_na}"
    assert vec_run.n_na > 0, "vector_only should skip historical queries"
    reclaimed = vec_run.n_na
    answered = len(bt_run.per_query)
    print(f"✓ Reclaims historical queries        (bi_temporal n_na=0 / {answered} scored; "
          f"vector_only n_na={reclaimed})")

    print(f"\nAll bi_temporal smoke checks green. {reclaimed} historical queries that are "
          f"N/A for every\nother backend are now answerable. Wire into run_w2 for the "
          f"current/historical split.")
