"""
GRAFOMEM GMP v0.1 reference implementation — the store the conformance suite certifies.

Each reference adapter the paper ships proves ONE axis: bi_temporal does
versioning, honest_delete does deletion, tenant_scoped does isolation. None passes
the whole suite. `GMPReferenceBackend` composes them on a single pinned BGE-small
vector core and declares — and PASSES — the full GMP v0.1 normative profile:

    {AUDIT, SUPERSESSION_CHAIN, BI_TEMPORAL, HARD_DELETE, MULTI_TENANT}

  - Versioning (GMP §3). Per-version valid-time [valid_from, valid_until); supersede
    closes the predecessor's interval at the successor's valid_from. retrieve(as_of=
    None) sees open heads (valid_until is None); retrieve(as_of=t) sees the version
    valid at t. (This is the bi_temporal adapter's mechanism.)
  - Deletion (GMP §6.2). delete() removes the fact from the store outright, so it is
    unrecoverable via retrieve() AND audit() — honest, and two-sided: survivors are
    untouched (no over-deletion).
  - Tenancy (GMP §6.3, D2). Every fact carries its owning tenant; retrieve scopes the
    candidate set to the querying tenant. Identity is tenant-scoped (§1.2), so a
    tenant is a PARTITION of the store rather than a filter over a shared pool.

The axes compose because the candidate filter is their conjunction —
not-deleted AND valid-at(as_of) AND owned-by(tenant) — and each workload activates
only the dimension it tests: single-tenant traces carry tenant_id=None on both the
fact and the querying session (the default namespace, §1.2), so the tenant predicate
is None==None there; un-versioned facts are open heads; un-deleted facts stay. The
retrieval RANKING is left to the embedder (GMP §4): swap the embedder and the safety
guarantees are unchanged (Proposition 2).

Its smoke runs the conformance suite on itself and asserts the full profile with no
violations — spec -> suite -> certified implementation, loop closed. Requires
grafomem[backends].
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import numpy as np

from aml.backends.interface import (
    Capability,
    Memory,
    RetrieveOptions,
    SourceMeta,
    WriteOptions,
)
from aml.backends.vector_only import REFERENCE_MODEL, EmbedFn, _default_embedder
from aml.provenance import fact_id_for_content, sign_provenance

__grafomem_interface__ = "0.1.1"

# The full GMP v0.1 normative profile (GMP §7.4). Reserved capabilities
# (CROSS_SESSION_PROPAGATION, CONFLICT_DETECTION, PROVENANCE,
# CRYPTOGRAPHIC_PROVENANCE) are intentionally NOT claimed in v0.1.
GMP_V01_PROFILE = frozenset({
    Capability.AUDIT,
    Capability.SUPERSESSION_CHAIN,
    Capability.BI_TEMPORAL,
    Capability.HARD_DELETE,
    Capability.MULTI_TENANT,
})

# GMP v0.2 adds provenance: PROVENANCE (normative — source round-trips through the
# store) and CRYPTOGRAPHIC_PROVENANCE (optional extension — Ed25519 over the content
# fact_id, §4.1). Still reserved for a design pass: CROSS_SESSION_PROPAGATION,
# CONFLICT_DETECTION.
GMP_V02_PROFILE = GMP_V01_PROFILE | {
    Capability.PROVENANCE,
    Capability.CRYPTOGRAPHIC_PROVENANCE,
}


class GMPReferenceBackend:
    """A single BGE vector store that honors versioning, deletion, and tenancy —
    the store-shaped answer to "what does GMP v0.1 require?"."""

    __grafomem_interface__ = "0.1.1"
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference",
        "embedding_model": REFERENCE_MODEL,
        "vector_store": "numpy-bruteforce-cosine-exact",
        "spec": "GMP v0.2",
        "profile": "v0.1 core + PROVENANCE + CRYPTOGRAPHIC_PROVENANCE",
        "identity": "tenant-scoped (GMP §1.2, D2)",
        "notes": "Composes versioning + deletion + tenancy on one BGE core; "
                 "ranking is embedder-pluggable (GMP §4).",
    }

    def __init__(self, embed_fn: EmbedFn | None = None) -> None:
        self._embed_fn = embed_fn
        self._store: dict[int, Memory] = {}
        self._vec: dict[int, np.ndarray] = {}
        self._vfrom: dict[int, datetime | None] = {}
        self._vuntil: dict[int, datetime | None] = {}   # None = open interval (head)
        self._next = 0

    def _embed(self, texts: list[str]) -> np.ndarray:
        if self._embed_fn is None:
            self._embed_fn = _default_embedder()
        return self._embed_fn(texts)

    def capabilities(self) -> set[Capability]:
        return set(GMP_V02_PROFILE)

    def write(self, content: str, options: WriteOptions) -> int:
        # All v0.2 caps are claimed: tenant_id / valid_from are honored, and every
        # write carries provenance (source). A signing_key additionally signs the
        # content fact_id (CRYPTOGRAPHIC_PROVENANCE).
        ref = self._next
        self._next += 1
        written_at = datetime.now(tz=timezone.utc)
        self._store[ref] = Memory(
            ref=ref, content=content,
            written_at=written_at,
            metadata=dict(options.metadata),
            valid_from=options.valid_from,
            tenant_id=options.tenant_id,
            source=self._make_source(ref, content, written_at, options),
        )
        self._vec[ref] = self._embed([content])[0]
        self._vfrom[ref] = options.valid_from
        self._vuntil[ref] = None                          # open until superseded
        return ref

    def _make_source(self, ref: int, content: str, written_at,
                     options: WriteOptions) -> SourceMeta:
        # PROVENANCE: write_id + written_at on every write. CRYPTOGRAPHIC_PROVENANCE:
        # given a signing_identity, sign the content fact_id and record the public key.
        src = SourceMeta(write_id=str(ref), written_at=written_at)
        if options.signing_identity is not None:
            fid = fact_id_for_content(content, options.tenant_id)
            src.signature, src.public_key = sign_provenance(options.signing_identity, fid)
            src.written_by = src.public_key.hex()
        return src

    def supersede(self, old_ref, content: str, options: WriteOptions) -> int:
        # The successor's valid_from closes the predecessor's interval (§3.2,
        # contiguous windows). If old_ref is unknown, behave as a fresh write (§6.3).
        new_ref = self.write(content, options)
        if old_ref in self._store:
            self._vuntil[old_ref] = options.valid_from
            self._store[old_ref].superseded_by = new_ref
        return new_ref

    def delete(self, ref) -> bool:
        # Honest hard delete (§6.2): exact removal -> unrecoverable via retrieve()
        # and audit(). Idempotent: False (no raise) on unknown/already-deleted refs.
        if ref not in self._store:
            return False
        del self._store[ref]
        del self._vec[ref]
        self._vfrom.pop(ref, None)
        self._vuntil.pop(ref, None)
        return True

    def _valid_at(self, ref: int, t: datetime | None) -> bool:
        if t is None:                                     # current: open heads only
            return self._vuntil[ref] is None
        vf, vu = self._vfrom[ref], self._vuntil[ref]
        return (vf is None or vf <= t) and (vu is None or t < vu)

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        t = options.as_of
        tenant = options.tenant_id
        # Candidate set = the conjunction of the three axes (deletion is implicit:
        # deleted refs are no longer in _store). Ranking is the embedder's job.
        refs = [
            r for r in self._store
            if self._valid_at(r, t) and self._store[r].tenant_id == tenant
        ]
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
        # All retained facts incl. superseded (valid_until set), excl. deleted (removed).
        return iter(list(self._store.values()))

    def flush(self) -> None:
        pass


# ============================================================================
# Smoke — run `python -m aml.backends.gmp_reference`
#
# Round-trips each axis, then certifies the backend against the conformance
# suite itself: spec -> suite -> certified implementation.
# ============================================================================

if __name__ == "__main__":
    from datetime import timedelta

    from aml.backends.interface import MemoryBackend
    from aml.backends.vector_only import _stub_embedder

    print("GRAFOMEM GMPReferenceBackend — full GMP v0.2 profile (STUB embedder)\n")

    b = GMPReferenceBackend(embed_fn=_stub_embedder())
    assert isinstance(b, MemoryBackend)
    assert b.capabilities() == set(GMP_V02_PROFILE)
    print("✓ capabilities                       "
          "(v0.1 core + PROVENANCE + CRYPTOGRAPHIC_PROVENANCE)")

    # Versioning + as_of, scoped to tenant A; tenant B holds its own fact.
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1, t2 = t0 + timedelta(days=30), t0 + timedelta(days=60)
    r0 = b.write("Aria lives in Rome", WriteOptions(valid_from=t0, tenant_id="A"))
    r1 = b.supersede(r0, "Aria lives in Milan", WriteOptions(valid_from=t1, tenant_id="A"))
    r2 = b.supersede(r1, "Aria lives in Turin", WriteOptions(valid_from=t2, tenant_id="A"))
    b.write("Bruno lives in Naples", WriteOptions(valid_from=t0, tenant_id="B"))
    b.flush()

    now = [m.content for m in b.retrieve(
        "Where does Aria live?", RetrieveOptions(as_of=None, tenant_id="A", budget_tokens=512))]
    assert now == ["Aria lives in Turin"], now
    past = [m.content for m in b.retrieve(
        "Where does Aria live?",
        RetrieveOptions(as_of=t1 + timedelta(days=5), tenant_id="A", budget_tokens=512))]
    assert past == ["Aria lives in Milan"], past
    print("✓ versioning + as_of                 (head = Turin; as_of(t1) = Milan)")

    # Tenant isolation: tenant B never sees tenant A's facts.
    bq = [m.content for m in b.retrieve(
        "Where does Aria live?", RetrieveOptions(tenant_id="B", budget_tokens=512))]
    assert all("Aria" not in c for c in bq), bq
    print("✓ tenant isolation                   (tenant B cannot see tenant A's facts)")

    # Honest delete: gone from retrieve AND audit; idempotent.
    assert b.delete(r2) is True
    b.flush()
    after = [m.content for m in b.retrieve(
        "Where does Aria live?", RetrieveOptions(tenant_id="A", budget_tokens=512))]
    assert "Aria lives in Turin" not in after
    assert all(m.content != "Aria lives in Turin" for m in b.audit())
    assert b.delete(r2) is False
    print("✓ honest delete                      (unrecoverable via retrieve + audit; idempotent)")

    # Provenance: every memory carries source; a signed write verifies, tampering fails.
    from aml.backends.interface import verify_provenance
    from aml.provenance import fact_id_for_content
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat,
    )

    bp = GMPReferenceBackend(embed_fn=_stub_embedder())
    key = Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    rp = bp.write("Aria prefers tea", WriteOptions(signing_identity=key, tenant_id="A"))
    bp.flush()
    m = next(m for m in bp.audit() if m.ref == rp)
    assert m.source is not None and m.source.write_id == str(rp)
    assert verify_provenance(m, fact_id_for_content(m.content, m.tenant_id)) is True
    assert verify_provenance(m, fact_id_for_content("Aria prefers coffee", m.tenant_id)) is False
    print("✓ provenance + signature             (source round-trips; signed verifies; tamper fails)")

    # The headline: certify against the conformance suite itself.
    from aml.eval.conformance import run_conformance

    profile = run_conformance(
        lambda: GMPReferenceBackend(embed_fn=_stub_embedder()),
        name="GMPReferenceBackend")
    print(f"\n  conformance profile -> SUPPORTS "
          f"{{{', '.join(sorted(c.value for c in profile.supported))}}}")
    missing = set(GMP_V02_PROFILE) - profile.supported
    assert not missing, f"does not pass: {missing}"
    assert not profile.violations, [r.capability.value for r in profile.violations]
    print("\n✓ PASSES the full GMP v0.2 conformance suite — no violations.")
    print("  spec -> suite -> certified reference implementation. Loop closed.")
