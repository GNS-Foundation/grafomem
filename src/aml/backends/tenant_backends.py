"""
GRAFOMEM tenant backends — the isolation spectrum (W5).

Three backends, all claiming {MULTI_TENANT, AUDIT} and sharing the same pinned
BGE store. They differ ONLY in which records a querying tenant is allowed to see
— which is exactly the point: each accepts a tenant_id on write and retrieve (the
type contract is identical and the MULTI_TENANT claim is honored at the API
surface), but the *isolation* contract diverges, and only the benchmark's
cross-tenant leakage / in-tenant recall checks tell them apart.

  - LeakyTenant   : stores each fact's owning tenant, but retrieve() ignores the
                    querying tenant and ranks over ALL tenants -> surfaces other
                    tenants' facts. Claims MULTI_TENANT, does not enforce it.
                    The tenancy analog of SoftDeleteBackend (claim != behavior).
                    Fails leakage.
  - TenantScoped  : retrieve() ranks only over the querying tenant's facts. No
                    cross-tenant leak; every in-tenant fact still reachable. The
                    correct one.
  - OverIsolating : scopes to the querying tenant AND hides any subject that
                    appears under more than one tenant ("ambiguous -> might
                    leak, so withhold"). An over-cautious privacy heuristic: no
                    leak, but it drops legitimate in-tenant facts. Because W5
                    shares every subject across tenants, in-tenant recall
                    craters — the tenancy analog of CoarseDeleteBackend.

The harness passes WriteOptions.tenant_id (the fact's owner) and
RetrieveOptions.tenant_id (the querying session's tenant) only to MULTI_TENANT
backends; the no-capability baseline (vector_only) sees tenant_id=None, pools all
tenants, and leaks. Requires grafomem[backends].
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


class _TenantVectorBase:
    """Shared BGE store tagging each record with its owning tenant + subject.
    Subclasses define which records a querying tenant may see."""

    __grafomem_interface__ = "0.1.1"

    def __init__(self, embed_fn: EmbedFn | None = None) -> None:
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._tenant: dict[int, str | None] = {}
        self._subject: dict[int, str | None] = {}
        self._next = 0

    def _embed(self, texts: list[str]) -> np.ndarray:
        if self._embed_fn is None:
            self._embed_fn = _default_embedder()
        return self._embed_fn(texts)

    def capabilities(self) -> set[Capability]:
        return {Capability.MULTI_TENANT, Capability.AUDIT}

    def write(self, content: str, options: WriteOptions) -> int:
        # MULTI_TENANT is claimed, so a tenant_id is accepted (not refused).
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
        self._tenant[ref] = options.tenant_id
        self._subject[ref] = options.metadata.get("subject")
        return ref

    def supersede(self, old_ref, content: str, options: WriteOptions) -> int:
        raise CapabilityNotSupported(Capability.SUPERSESSION_CHAIN, "supersede")

    def delete(self, ref) -> bool:
        raise CapabilityNotSupported(Capability.HARD_DELETE, "delete")

    # Subclasses override: which refs are visible to a query from `tenant`.
    def _visible_refs(self, tenant: str | None) -> list[int]:
        raise NotImplementedError

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        if options.as_of is not None:
            raise CapabilityNotSupported(Capability.BI_TEMPORAL, "retrieve")
        # MULTI_TENANT claimed: a tenant_id is honored, not refused.
        refs = self._visible_refs(options.tenant_id)
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
        return iter(list(self._store.values()))

    def flush(self) -> None:
        pass


class LeakyTenant(_TenantVectorBase):
    """Stores the owning tenant but ignores it on retrieve -> ranks over all
    tenants and surfaces other tenants' facts. Claims MULTI_TENANT, doesn't
    enforce it (the tenancy analog of soft_delete)."""

    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "isolation_policy": "tenant tagged on write; NOT enforced on retrieve (leaks)",
    }

    def _visible_refs(self, tenant: str | None) -> list[int]:
        return list(self._store.keys())          # BUG: tenant ignored -> leaks


class TenantScoped(_TenantVectorBase):
    """Retrieve ranks only over the querying tenant's records. Correct."""

    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "isolation_policy": "retrieve scoped to querying tenant",
    }

    def _visible_refs(self, tenant: str | None) -> list[int]:
        return [r for r in self._store if self._tenant[r] == tenant]


class OverIsolating(_TenantVectorBase):
    """Scopes to the querying tenant AND withholds any subject that appears
    under more than one tenant ('ambiguous -> might leak'). No leak, but it
    drops legitimate in-tenant facts (the tenancy analog of coarse_delete)."""

    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference", "embedding_model": REFERENCE_MODEL,
        "isolation_policy": "tenant-scoped + hide subjects shared across tenants "
                            "(over-cautious; over-isolates)",
    }

    def _visible_refs(self, tenant: str | None) -> list[int]:
        subj_tenants: dict[str | None, set[str | None]] = {}
        for r in self._store:
            subj_tenants.setdefault(self._subject[r], set()).add(self._tenant[r])
        return [r for r in self._store
                if self._tenant[r] == tenant
                and len(subj_tenants[self._subject[r]]) == 1]


# ============================================================================
# Smoke check — run `python -m aml.backends.tenant_backends`
# ============================================================================

if __name__ == "__main__":
    from aml.backends.interface import MemoryBackend
    from aml.backends.vector_only import _stub_embedder

    print("GRAFOMEM tenant_backends.py — the isolation spectrum (STUB)\n")

    def fresh(cls):
        b = cls(embed_fn=_stub_embedder())
        # Kesia is SHARED across both tenants (distinct objects); Zara is unique
        # to tenant-a, Yuki unique to tenant-b.
        b.write("Kesia is located in Marrowfen",
                WriteOptions(tenant_id="tenant-a", metadata={"subject": "Kesia"}))
        b.write("Zara is located in Aria",
                WriteOptions(tenant_id="tenant-a", metadata={"subject": "Zara"}))
        b.write("Kesia is located in Highford",
                WriteOptions(tenant_id="tenant-b", metadata={"subject": "Kesia"}))
        b.write("Yuki is located in Bram",
                WriteOptions(tenant_id="tenant-b", metadata={"subject": "Yuki"}))
        return b

    def hits(b, query, tenant):
        return {m.content for m in b.retrieve(
            query, RetrieveOptions(budget_tokens=512, tenant_id=tenant))}

    for cls in (LeakyTenant, TenantScoped, OverIsolating):
        b = fresh(cls)
        assert isinstance(b, MemoryBackend)
        assert b.capabilities() == {Capability.MULTI_TENANT, Capability.AUDIT}
    print("✓ All three: Protocol + {MULTI_TENANT, AUDIT}")

    Q = "Where is Kesia located?"

    # leaky: tenant-a's Kesia query surfaces tenant-b's Kesia (Highford) -> LEAK
    b = fresh(LeakyTenant)
    got = hits(b, Q, "tenant-a")
    assert "Kesia is located in Highford" in got, "leaky should surface other tenant's Kesia"
    print("✓ LeakyTenant    LEAKS         (tenant-a's Kesia query returns tenant-b's Highford)")

    # scoped: tenant-a sees only its own Kesia (Marrowfen); never Highford.
    b = fresh(TenantScoped)
    got = hits(b, Q, "tenant-a")
    assert "Kesia is located in Marrowfen" in got, "scoped dropped own Kesia"
    assert "Kesia is located in Highford" not in got, "scoped leaked tenant-b's Kesia"
    assert "Zara is located in Aria" in hits(b, "Where is Zara located?", "tenant-a")
    print("✓ TenantScoped   CLEAN         (own Kesia only; no cross-tenant Highford; own Zara intact)")

    # over-isolating: Kesia is shared -> withheld from BOTH owners; unique Zara kept.
    b = fresh(OverIsolating)
    got = hits(b, Q, "tenant-a")
    assert "Kesia is located in Marrowfen" not in got, "over-isolating should drop shared Kesia"
    assert "Kesia is located in Highford" not in got, "over-isolating must not leak either"
    assert "Zara is located in Aria" in hits(b, "Where is Zara located?", "tenant-a"), \
        "over-isolating wrongly dropped tenant-unique Zara"
    print("✓ OverIsolating  OVER-ISOLATES (no leak, but drops shared-subject Kesia from its OWN tenant; unique Zara kept)")

    # guards: as_of / supersede / delete refused
    b = fresh(TenantScoped)
    for op, call in (
        ("as_of", lambda: b.retrieve("x", RetrieveOptions(
            as_of=datetime.now(tz=timezone.utc), tenant_id="tenant-a"))),
        ("supersede", lambda: b.supersede(0, "x", WriteOptions(tenant_id="tenant-a"))),
        ("delete", lambda: b.delete(0)),
    ):
        try:
            call()
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(f"{op}: expected CapabilityNotSupported")
    print("✓ Capability guards (as_of/supersede/delete refused)")

    print("\nAll tenant_backends smoke checks green. Next: run_w5 (M7 cross-tenant "
          "leakage + in-tenant recall) on the real BGE store.")
