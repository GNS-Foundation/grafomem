"""
GRAFOMEM conflict backends — the conflict-resolution spectrum (W7).

Six toy backends, all stub-embeddable and sharing the same store, that each
exhibit ONE of the six W7 behavior classes (01-workload-spec.md §4.7). They
differ only in how they handle two writes to the same (subject, predicate) —
which is exactly the variable W7 measures. The contested slot is keyed off the
structured `{"subject", "predicate"}` metadata the harness passes on every
write (WriteOptions.metadata), the same channel the W6 coarse-delete backend
uses.

  - MergeBackend          : keeps every write; both contested values surface. -> merge
  - LastWriteWinsBackend  : a colliding write drops the prior value for that
                            slot, keeps the new.                       -> last_write_wins
  - FirstWriteWinsBackend : keeps the first write to a slot, silently ignores
                            later ones.                                -> first_write_wins
  - SilentDataLossBackend : a collision drops the existing value AND ignores the
                            new — the slot goes empty.                 -> silent_data_loss
  - ConflictAwareBackend  : keeps both AND surfaces the conflict via
                            metadata["conflict"]=True when a retrieved slot holds
                            more than one value. The ONLY one claiming
                            CONFLICT_DETECTION.                        -> conflict_flag
  - FlakyBackend          : keeps both, but each retrieve randomly returns one
                            value per slot, so answers flip across runs (seeded
                            per instance to model a stochastic backend). -> non_deterministic

A backend that ignores a write must still return a ref so the harness's
ref->fact_id ledger stays consistent; we hand back a "phantom" ref (allocated,
never stored) in that case — the fact maps to a ref that is simply never
retrieved.

Stub-embeddable like the delete backends: W7 measures conflict RESOLUTION, not
embedding quality, so the deterministic stub embedder is sufficient and the
real BGE model is unnecessary. Requires numpy.
"""

from __future__ import annotations

import dataclasses
import random
from collections import defaultdict
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


class _ConflictVectorBase:
    """Shared stub-embeddable store; subclasses define collision handling
    (`_resolve_write`) and/or a retrieval post-pass (`_post_retrieve`)."""

    __grafomem_interface__ = "0.1.1"

    def __init__(self, embed_fn: EmbedFn | None = None) -> None:
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._key_refs: dict[tuple, list[int]] = {}   # (subject, predicate) -> refs
        self._next = 0

    def _embed(self, texts: list[str]) -> np.ndarray:
        if self._embed_fn is None:
            self._embed_fn = _default_embedder()
        return self._embed_fn(texts)

    def capabilities(self) -> set[Capability]:
        return {Capability.AUDIT}

    @staticmethod
    def _key(options: WriteOptions) -> tuple:
        return (options.metadata.get("subject"), options.metadata.get("predicate"))

    def _alloc(self) -> int:
        ref = self._next
        self._next += 1
        return ref

    def _store_write(self, content: str, options: WriteOptions) -> int:
        ref = self._alloc()
        self._store[ref] = Memory(
            ref=ref, content=content,
            written_at=datetime.now(tz=timezone.utc),
            metadata=dict(options.metadata),
        )
        self._vec[ref] = self._embed([content])[0]
        self._key_refs.setdefault(self._key(options), []).append(ref)
        return ref

    def _drop_key(self, key: tuple) -> None:
        for r in self._key_refs.get(key, []):
            self._store.pop(r, None)
            self._vec.pop(r, None)
        self._key_refs[key] = []

    # ---- contract -------------------------------------------------------
    def write(self, content: str, options: WriteOptions) -> int:
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "write")
        if options.signing_key is not None:
            raise CapabilityNotSupported(Capability.CRYPTOGRAPHIC_PROVENANCE, "write")
        return self._resolve_write(content, options)

    def _resolve_write(self, content: str, options: WriteOptions) -> int:
        return self._store_write(content, options)        # default: keep everything

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
        order = sorted(range(len(refs)), key=lambda i: (-float(sims[i]), refs[i]))
        out: list[Memory] = []
        used = 0
        for i in order:
            if sims[i] <= 0.0:                 # no lexical overlap -> irrelevant
                continue
            m = self._store[refs[i]]
            cost = len(m.content)
            if used + cost > options.budget_tokens:
                break
            out.append(m)
            used += cost
        return self._post_retrieve(out)

    def _post_retrieve(self, mems: list[Memory]) -> list[Memory]:
        return mems

    def audit(self) -> Iterator[Memory]:
        return iter(list(self._store.values()))

    def flush(self) -> None:
        pass


def _by_slot(mems: list[Memory]) -> dict[tuple, list[Memory]]:
    groups: dict[tuple, list[Memory]] = defaultdict(list)
    for m in mems:
        groups[(m.metadata.get("subject"), m.metadata.get("predicate"))].append(m)
    return groups


class MergeBackend(_ConflictVectorBase):
    """Keeps every write; both contested values surface together. -> merge."""
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "conflict_policy": "keep all writes (no resolution)",
    }


class LastWriteWinsBackend(_ConflictVectorBase):
    """A colliding write drops the prior value for that slot. -> last_write_wins."""
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "conflict_policy": "last write wins (overwrite per slot)",
    }

    def _resolve_write(self, content, options):
        self._drop_key(self._key(options))
        return self._store_write(content, options)


class FirstWriteWinsBackend(_ConflictVectorBase):
    """Keeps the first write to a slot; silently ignores later ones. -> first_write_wins."""
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "conflict_policy": "first write wins (ignore later)",
    }

    def _resolve_write(self, content, options):
        if self._key_refs.get(self._key(options)):
            return self._alloc()               # phantom: ignore, but keep the ledger honest
        return self._store_write(content, options)


class SilentDataLossBackend(_ConflictVectorBase):
    """A collision drops the existing value AND ignores the new — slot empties. -> silent_data_loss."""
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "conflict_policy": "collision wipes both values (silent loss)",
    }

    def _resolve_write(self, content, options):
        key = self._key(options)
        if self._key_refs.get(key):
            self._drop_key(key)                # lose the old...
            return self._alloc()               # ...and the new (phantom)
        return self._store_write(content, options)


class ConflictAwareBackend(_ConflictVectorBase):
    """Keeps both AND surfaces the conflict (metadata['conflict']=True) when a
    retrieved slot holds more than one value. The only CONFLICT_DETECTION
    backend. -> conflict_flag."""
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "conflict_policy": "keep all; surface conflict marker on contested slots",
    }

    def capabilities(self) -> set[Capability]:
        return {Capability.AUDIT, Capability.CONFLICT_DETECTION}

    def _post_retrieve(self, mems):
        groups = _by_slot(mems)
        out: list[Memory] = []
        for m in mems:
            slot = (m.metadata.get("subject"), m.metadata.get("predicate"))
            if len(groups[slot]) > 1:          # contested -> mark a COPY (store stays clean)
                out.append(dataclasses.replace(m, metadata={**m.metadata, "conflict": True}))
            else:
                out.append(m)
        return out


class FlakyBackend(_ConflictVectorBase):
    """Keeps both, but each retrieve randomly returns one value per slot, so
    answers flip across runs. Seeded per instance to model a stochastic backend
    (the runner varies the seed across replays). -> non_deterministic."""
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "conflict_policy": "keep all; return a random one per slot (unstable)",
    }

    def __init__(self, embed_fn: EmbedFn | None = None, seed: int = 0) -> None:
        super().__init__(embed_fn)
        self._rng = random.Random(seed)

    def _post_retrieve(self, mems):
        groups = _by_slot(mems)
        keep = {self._rng.choice(g).ref for g in groups.values()}
        return [m for m in mems if m.ref in keep]


# ============================================================================
# Smoke check — run `python -m aml.backends.conflict_backends`
# ============================================================================

if __name__ == "__main__":
    from aml.backends.interface import MemoryBackend
    from aml.backends.vector_only import _stub_embedder

    print("GRAFOMEM conflict_backends.py — the conflict-resolution spectrum (STUB)\n")

    SUBJ, PRED = "Aria", "lives_in"
    A_TEXT, B_TEXT = "Aria lives in Rome", "Aria lives in Milan"

    def fresh(cls, **kw):
        b = cls(embed_fn=_stub_embedder(), **kw)
        meta = WriteOptions(metadata={"subject": SUBJ, "predicate": PRED})
        b.write(A_TEXT, meta)          # earlier
        b.write(B_TEXT, meta)          # later (the collision)
        return b

    def returned(b):
        mems = b.retrieve("Where does Aria live?", RetrieveOptions(budget_tokens=1 << 20))
        texts = {m.content for m in mems}
        signaled = any(m.metadata.get("conflict") for m in mems)
        return texts, signaled

    for cls in (MergeBackend, LastWriteWinsBackend, FirstWriteWinsBackend,
                SilentDataLossBackend, ConflictAwareBackend, FlakyBackend):
        assert isinstance(cls(embed_fn=_stub_embedder()), MemoryBackend)
    print("✓ All six implement MemoryBackend Protocol")

    txt, sig = returned(fresh(MergeBackend))
    assert txt == {A_TEXT, B_TEXT} and not sig, (txt, sig)
    print(f"✓ merge              returns both       ({len(txt)} values, conflict={sig})")

    txt, sig = returned(fresh(LastWriteWinsBackend))
    assert txt == {B_TEXT} and not sig, (txt, sig)
    print(f"✓ last_write_wins    keeps the later     ({txt})")

    txt, sig = returned(fresh(FirstWriteWinsBackend))
    assert txt == {A_TEXT} and not sig, (txt, sig)
    print(f"✓ first_write_wins   keeps the earlier   ({txt})")

    txt, sig = returned(fresh(SilentDataLossBackend))
    assert txt == set() and not sig, (txt, sig)
    print(f"✓ silent_data_loss   loses both          ({txt or '{}'})")

    txt, sig = returned(fresh(ConflictAwareBackend))
    assert txt == {A_TEXT, B_TEXT} and sig, (txt, sig)
    assert ConflictAwareBackend(embed_fn=_stub_embedder()).capabilities() == {
        Capability.AUDIT, Capability.CONFLICT_DETECTION}
    print(f"✓ conflict_aware     both + conflict flag (conflict={sig}; claims CONFLICT_DETECTION)")

    # flaky: one value per slot, and two different seeds disagree on the pick.
    t0, _ = returned(fresh(FlakyBackend, seed=0))
    t1, _ = returned(fresh(FlakyBackend, seed=1))
    assert len(t0) == 1 and len(t1) == 1, (t0, t1)
    flips = any(returned(fresh(FlakyBackend, seed=s))[0] != t0 for s in range(1, 12))
    assert flips, "flaky never flipped across seeds"
    print(f"✓ flaky              one value, flips     (seed0={t0}, differs across seeds)")

    # guards
    b = fresh(MergeBackend)
    for op, call in (
        ("supersede", lambda: b.supersede(0, "x", WriteOptions())),
        ("delete", lambda: b.delete(0)),
        ("as_of", lambda: b.retrieve("x", RetrieveOptions(as_of=datetime.now(tz=timezone.utc)))),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Capability guards                  (supersede / delete / as_of refused)")

    print("\nAll conflict_backends smoke checks green. Six classes embodied; "
          "run_w7 drives them through the classifier.")
