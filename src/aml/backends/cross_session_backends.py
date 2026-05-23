"""
GRAFOMEM cross-session backends — the deletion-PROPAGATION spectrum (W9).

W9 asks whether "forget" is global across the sessions of one backend instance.
Every cluster here is N session handles over a SINGLE shared BGE store — writes
and reads are global (a fact written in any session is visible in all; that is
what "the same backend instance" means). The handles differ in exactly one
thing — what delete() propagates — which is precisely the capability under test:

  - propagating   : delete() removes the fact from the shared store -> it is
                    gone in every session. Claims and HONORS
                    CROSS_SESSION_PROPAGATION. The correct one.
  - session_local : delete() records a tombstone visible to the ISSUING session
                    only. The fact is filtered there but RESURFACES when probed
                    from another session. Claims CROSS_SESSION_PROPAGATION yet
                    fails the semantic contract — the W9 finding (claim != honor),
                    the cross-session sibling of W6's soft_delete.
  - no_propagation: honest global removal, but does NOT claim
                    CROSS_SESSION_PROPAGATION -> run_w9 SKIPS it (a backend that
                    cannot make the guarantee is not scored; spec §4.9).

A capability *claim* does not certify propagation: session_local claims
CROSS_SESSION_PROPAGATION and returns success from delete(), yet leaks across
sessions. Only the cross-session probe (delete in B, ask in C) tells them apart.

delete(ref) carries no session context (interface.py §5), so the runner — not
the backend — owns which session each operation belongs to: run_w9 dispatches
every turn to cluster[session_index]. interface.py / harness.py are untouched.
Requires grafomem[backends] for the real BGE; the smoke uses the stub embedder.
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


class _SharedVectorStore:
    """One pinned-BGE store shared by every session handle in a cluster.

    Mirrors VectorOnlyBackend's exact-cosine mechanics. Writes/reads are global;
    `remove` is a true hard removal (store + index). Deletion *propagation* is a
    handle concern, not a store concern — the store only knows how to remove.
    """

    def __init__(self, embed_fn: EmbedFn | None = None) -> None:
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._next = 0

    def _embed(self, texts: list[str]) -> np.ndarray:
        if self._embed_fn is None:
            self._embed_fn = _default_embedder()
        return self._embed_fn(texts)

    def write(self, content: str, options: WriteOptions) -> int:
        ref = self._next
        self._next += 1
        self._store[ref] = Memory(
            ref=ref, content=content,
            written_at=datetime.now(tz=timezone.utc),
            metadata=dict(options.metadata),
        )
        self._vec[ref] = self._embed([content])[0]
        return ref

    def exists(self, ref: int) -> bool:
        return ref in self._store

    def remove(self, ref: int) -> bool:
        if ref in self._store:
            del self._store[ref]
            del self._vec[ref]
            return True
        return False

    def retrieve(self, query: str, options: RetrieveOptions,
                 *, hidden: set[int]) -> list[Memory]:
        refs = [r for r in self._store if r not in hidden]
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

    def all_memories(self, hidden: set[int]) -> list[Memory]:
        return [self._store[r] for r in self._store if r not in hidden]


class _SessionHandle:
    """One session's MemoryBackend view onto a shared cluster store.

    propagate_delete=True : delete() removes from the shared store -> gone in
        every session (honors CROSS_SESSION_PROPAGATION).
    propagate_delete=False: delete() tombstones in THIS handle only -> the fact
        resurfaces when probed from another session (the propagation violation).
    """

    __grafomem_interface__ = "0.1.1"

    def __init__(self, store: _SharedVectorStore, caps: set[Capability],
                 *, propagate_delete: bool) -> None:
        self._store = store
        self._caps = frozenset(caps)
        self._propagate = propagate_delete
        self._local_tombstones: set[int] = set()   # used only when not propagating

    def capabilities(self) -> set[Capability]:
        return set(self._caps)

    def write(self, content: str, options: WriteOptions) -> int:
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "write")
        if options.signing_key is not None:
            raise CapabilityNotSupported(
                Capability.CRYPTOGRAPHIC_PROVENANCE, "write")
        return self._store.write(content, options)

    def supersede(self, old_ref, content: str, options: WriteOptions) -> int:
        raise CapabilityNotSupported(Capability.SUPERSESSION_CHAIN, "supersede")

    def delete(self, ref) -> bool:
        if self._propagate:
            return self._store.remove(ref)              # global hard removal
        if self._store.exists(ref):
            self._local_tombstones.add(ref)            # session-scoped; no propagation
            return True
        return False

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        if options.as_of is not None:
            raise CapabilityNotSupported(Capability.BI_TEMPORAL, "retrieve")
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "retrieve")
        return self._store.retrieve(query, options, hidden=self._local_tombstones)

    def audit(self) -> Iterator[Memory]:
        return iter(self._store.all_memories(self._local_tombstones))

    def flush(self) -> None:
        pass


# Honors propagation: shared store + global removal + the capability claim.
_PROPAGATING_CAPS = {
    Capability.HARD_DELETE, Capability.CROSS_SESSION_PROPAGATION, Capability.AUDIT,
}
# Claims HARD_DELETE but NOT cross-session propagation -> skipped by run_w9.
_BASELINE_CAPS = {Capability.HARD_DELETE, Capability.AUDIT}

_KINDS = ("propagating", "session_local", "no_propagation")


def make_cluster(kind: str, n_sessions: int,
                 embed_fn: EmbedFn | None = None) -> list[_SessionHandle]:
    """Build an N-session cluster of one of the three kinds. All handles share a
    single store; they differ only in delete propagation and declared caps."""
    store = _SharedVectorStore(embed_fn)
    if kind == "propagating":
        return [_SessionHandle(store, _PROPAGATING_CAPS, propagate_delete=True)
                for _ in range(n_sessions)]
    if kind == "session_local":
        return [_SessionHandle(store, _PROPAGATING_CAPS, propagate_delete=False)
                for _ in range(n_sessions)]
    if kind == "no_propagation":
        return [_SessionHandle(store, _BASELINE_CAPS, propagate_delete=True)
                for _ in range(n_sessions)]
    raise ValueError(f"unknown cluster kind {kind!r}; expected one of {_KINDS}")


# ============================================================================
# Smoke check — run `python -m aml.backends.cross_session_backends`
# ============================================================================

if __name__ == "__main__":
    from aml.backends.interface import MemoryBackend
    from aml.backends.vector_only import _stub_embedder

    print("GRAFOMEM cross_session_backends.py — the propagation spectrum (STUB)\n")

    def build(kind):
        # 3 sessions. Write Alice's two facts in session 0; we'll delete one in
        # session 1 and probe from session 2.
        c = make_cluster(kind, 3, embed_fn=_stub_embedder())
        a = c[0].write("Alice lives in Rome", WriteOptions(metadata={"subject": "Alice"}))
        b = c[0].write("Alice works at Acme", WriteOptions(metadata={"subject": "Alice"}))
        return c, a, b

    def has(handle, needle, query):
        return any(needle in m.content
                   for m in handle.retrieve(query, RetrieveOptions(budget_tokens=1 << 20)))

    # all handles satisfy the Protocol
    for kind in _KINDS:
        c, _, _ = build(kind)
        assert all(isinstance(h, MemoryBackend) for h in c)
    print("✓ All three kinds implement MemoryBackend Protocol (per session handle)")

    # propagating: delete in s1, gone in s2 (and s1, s0). Survivor intact everywhere.
    c, a, b = build("propagating")
    assert c[0].capabilities() == _PROPAGATING_CAPS
    assert c[1].delete(a) is True
    assert not has(c[2], "Alice lives in Rome", "Where does Alice live?"), \
        "propagating must NOT leak across sessions"
    assert not has(c[1], "Alice lives in Rome", "Where does Alice live?")
    assert has(c[2], "Alice works at Acme", "Where does Alice work?"), \
        "propagating dropped the survivor (cross-session read failed)"
    print("✓ propagating    CLEAN  (delete in s1 -> gone in s2; survivor readable cross-session)")

    # session_local: delete in s1 hides only in s1; LEAKS when probed from s2.
    c, a, b = build("session_local")
    assert Capability.CROSS_SESSION_PROPAGATION in c[1].capabilities()  # it CLAIMS it
    assert c[1].delete(a) is True
    assert not has(c[1], "Alice lives in Rome", "Where does Alice live?"), \
        "session_local should filter in the deleting session"
    assert has(c[2], "Alice lives in Rome", "Where does Alice live?"), \
        "session_local must LEAK across sessions (the W9 finding)"
    assert has(c[2], "Alice works at Acme", "Where does Alice work?")
    print("✓ session_local  LEAKS  (delete in s1 filtered in s1, but RESURFACES in s2 — claim != honor)")

    # no_propagation: behaves correctly but does not CLAIM the capability.
    c, a, b = build("no_propagation")
    assert Capability.CROSS_SESSION_PROPAGATION not in c[0].capabilities()
    print("✓ no_propagation SKIP   (no CROSS_SESSION_PROPAGATION claim -> run_w9 skips it; §4.9)")

    # guards
    c, a, b = build("propagating")
    for op, call in (
        ("supersede", lambda: c[0].supersede(0, "x", WriteOptions())),
        ("as_of", lambda: c[0].retrieve("x", RetrieveOptions(as_of=datetime.now(tz=timezone.utc)))),
        ("tenant", lambda: c[0].retrieve("x", RetrieveOptions(tenant_id="A"))),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Capability guards     (supersede / as_of / tenant refused)")

    print("\nAll cross_session_backends smoke checks green. "
          "Next: run_w9 (the propagation map).")
