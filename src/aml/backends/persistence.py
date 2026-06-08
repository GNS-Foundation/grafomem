"""
GRAFOMEM persistence baseline — the deployment floor (03-eval-metrics.md §8).

The naive "last-N-turns" memory: stores every write verbatim and, on retrieve,
returns the MOST RECENT memories that fit in budget_tokens — in recency order,
ignoring the query entirely. No semantic indexing, no temporal awareness, no
tenant isolation, no deletion. It claims NO capabilities (`set()`).

This is the floor every real architecture must beat by >=20% (E2). Because it
retrieves by recency alone, a fact queried long after it was introduced has
been pushed out of the window, so M1 typically lands at 0.2-0.4 — high on
short-horizon recall, collapsing as the horizon grows. That failure mode is
the baseline's reason for existing: it shows exactly what naive context
management buys you, and nothing more.

Budget is enforced as a character count (a spec-sanctioned token proxy, doc 02
§6.5); the harness re-counts with cl100k_base for M3 reporting separately.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from aml.backends.interface import (
    Capability,
    CapabilityNotSupported,
    Memory,
    RetrieveOptions,
    WriteOptions,
)

__grafomem_interface__ = "0.1.1"


class PersistenceBackend:
    """Last-N-turns recency floor. Claims nothing."""

    __grafomem_interface__ = "0.1.1"

    def __init__(self) -> None:
        self._store: dict[int, Memory] = {}
        self._order: list[int] = []       # write order; tail = most recent
        self._next = 0

    def capabilities(self) -> set[Capability]:
        return set()                       # the floor claims nothing

    def write(self, content: str, options: WriteOptions) -> int:
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "write")
        if options.signing_identity is not None:
            raise CapabilityNotSupported(
                Capability.CRYPTOGRAPHIC_PROVENANCE, "write")
        ref = self._next
        self._next += 1
        self._store[ref] = Memory(
            ref=ref,
            content=content,
            written_at=datetime.now(tz=timezone.utc),
            metadata=dict(options.metadata),
        )
        self._order.append(ref)
        return ref

    def supersede(self, old_ref, content, options):
        raise CapabilityNotSupported(Capability.SUPERSESSION_CHAIN, "supersede")

    def delete(self, ref) -> bool:
        raise CapabilityNotSupported(Capability.HARD_DELETE, "delete")

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        # The floor claims neither BI_TEMPORAL nor MULTI_TENANT.
        if options.as_of is not None:
            raise CapabilityNotSupported(Capability.BI_TEMPORAL, "retrieve")
        if options.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "retrieve")
        # Pure recency: walk newest -> oldest, take what fits the budget.
        # The query is deliberately ignored — that is the whole point.
        out: list[Memory] = []
        used = 0
        for ref in reversed(self._order):
            m = self._store[ref]
            cost = len(m.content)
            if used + cost > options.budget_tokens:
                break
            out.append(m)
            used += cost
        return out

    def audit(self) -> Iterator[Memory]:
        # AUDIT not claimed; the harness will not call this on the floor.
        raise CapabilityNotSupported(Capability.AUDIT, "audit")

    def flush(self) -> None:
        pass


# ============================================================================
# Smoke check — run `python -m aml.backends.persistence`
# ============================================================================

if __name__ == "__main__":
    print("GRAFOMEM persistence.py — last-N-turns floor\n")

    b = PersistenceBackend()
    assert b.capabilities() == set()
    print("✓ Claims no capabilities            (set())")

    refs = [b.write(f"fact number {i:02d}", WriteOptions()) for i in range(10)]
    b.flush()
    # Each content is 14 chars ("fact number NN"). Budget 40 -> last 2 fit.
    got = b.retrieve("anything at all", RetrieveOptions(budget_tokens=40))
    assert [m.ref for m in got] == [9, 8], f"expected newest-first [9,8]; got {[m.ref for m in got]}"
    print("✓ Recency retrieval, newest first    (budget 40 -> refs 9,8)")

    # Query text is ignored — different query, same recency result.
    got2 = b.retrieve("totally different query", RetrieveOptions(budget_tokens=40))
    assert [m.ref for m in got2] == [9, 8]
    print("✓ Query ignored                      (recency only, query-independent)")

    # Generous budget -> everything, still newest-first.
    allm = b.retrieve("x", RetrieveOptions(budget_tokens=10_000))
    assert [m.ref for m in allm] == list(reversed(refs))
    print("✓ Generous budget -> full window     (all 10, newest-first)")

    # Unclaimed surfaces raise.
    for op, call in (
        ("supersede", lambda: b.supersede(0, "x", WriteOptions())),
        ("delete", lambda: b.delete(0)),
        ("audit", lambda: list(b.audit())),
        ("as_of", lambda: b.retrieve("x", RetrieveOptions(
            as_of=datetime.now(tz=timezone.utc)))),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Unclaimed surfaces raise           (supersede/delete/audit/as_of)")

    print("\nAll persistence smoke checks green. Ready for the M1 harness.")
