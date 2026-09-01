---
status: proposed
record_date: 2026-09-01
provenance: raised-from-implementation (GNS-Foundation/geiant @geiant/core v4 support, surfaced while wiring the enforcing-mode seek seam per the v4 reference verifier)
scope: (implementation) @geiant/core CGR v4 consumer + its SupabaseRegistry store; (standard) a rollout/issuance prerequisite in the 0006 enforcement-boundary lineage
---

# 0007 — @geiant/core has no reverse edge index: enforcing-mode v4 verification is not implementable against its store

- **Status:** **Proposed** 2026-09-01 — records a finding and the immediate engineering decision it
  forces (**absent-entirely**, below). The broader questions it raises — which index design closes
  the gap, and whether issuance is gated on it — are left **open** for a Foundation decision.
- **Record date:** 2026-09-01
- **Depends on:** [[0006]] (the enforcement boundary; §3/§4 "enforcement index is the query surface").

## Context

The v4 reference verifier (`clients/cgr-verify`) resolves the enforcement boundary of [0006] into a
concrete interface: enforcing mode takes an injected

```
seek: async (subjectFingerprintHex) => attestation[]
```

that returns the Foundation-signed **edge-records** (a `revokes`/`supersedes` attestation whose
`relates_to` targets the subject's BLAKE2b-256 fingerprint). The verifier honours what `seek` returns
exactly as it honours *held* edges; a `seek` that throws yields `revocation status undeterminable`
and **fails closed** (Validity-Fails-Closed). The reference's own harness note names the second
consumer explicitly: this is "the same interface a real consumer (@geiant/core, read surface)
implements against its own store/index."

Wiring that against @geiant/core's real store surfaced the finding.

**@geiant/core has no store surface `seek` can query.** Its store (`SupabaseRegistry`) is a single
`agents` table keyed by `public_key`. A CGR attestation is stored *inline* on the agent row
(`cgr_attestation` jsonb) — the agent's *own* attestation. There is:

- **no attestation table addressable by hash / fingerprint**,
- **no `relates_to` persisted** anywhere, and
- **no reverse index** answering "which edge-records target fingerprint X?".

So `seek(fp)` has nothing to resolve against. The interface *shape* is sound — it is a query that
returns whole rows, which is what re-verification of each edge's signature requires, and geiant's
jsonb column is a compatible row shape — but it **presumes a reverse index (`target_fp →
edge-records`) that the consumer does not have**. Under expand-contract, issuance emits no v4 edges
yet, so even a correct index would be empty today.

## Decision

**In @geiant/core, the store-backed enforcing `seek` is absent entirely.** The package exposes the
injected-resolver interface (so a caller *may* supply its own `seek`, as the conformance harness does
with an in-memory ledger scan), but ships **no** store-backed resolver. Consequently an enforcing
verifier **cannot be constructed against geiant's store** — the capability is not expressible in the
API, so it cannot be shipped, misconfigured, or silently degrade.

Rejected alternative — **a store-backed resolver that returns `[]`**: a verifier claiming enforcing
mode while enforcing nothing is precisely the posture [0006] rejects — *worse* than non-enforcing,
because it looks like enforcement. Rejected.

Rejected alternative — **a store-backed resolver that throws** (→ fail-closed on every subject): more
honest than returning `[]`, but it still lets a caller *construct and ship* an "enforcing verifier
against geiant's store"; the impossibility only surfaces at query time as universal deny-all —
outage-shaped behaviour that reads as "working securely" (the same 0006 anti-pattern, inverted). Its
one merit — exercising the Validity-Fails-Closed path against a real resolver — is already covered by
corpus vector **L3** (`seek_fails=true`) plus a throwing-stub unit test. Absent-entirely makes the
impossibility a property of the type system instead of a runtime surprise; the compiler enforces the
prerequisite. Rejected in favour of absent-entirely.

Non-enforcing v4 verification (schema gate, `relates_to` validation, `continues`/validity traversal,
held-edge honouring, grounding gate) **is** implemented and works against the store today — that is
the "accept v4 and traverse before issuance emits anything" half of expand-contract, and it needs no
`seek`.

## Consequences

- **This is a previously-unidentified prerequisite for v4 issuance.** [0006] settled *that* revocation
  binds where someone runs the query; it did not surface that **the primary identity-holding consumer
  has no query surface to run**. Any rollout plan that assumes @geiant/core can enforce revocation
  once issuance begins is wrong until a reverse index exists. Issuance of `revokes`/`supersedes`
  edges without a consumer index means those edges bind *nowhere* that matters — the exact silent
  gap 0006 exists to prevent.
- Enforcing mode is exercised end-to-end **only by the conformance corpus** (in-memory `seek`), which
  proves interface compatibility, not store readiness.

## Open questions

1. **Which index design closes the gap?** Two candidates:
   - **(b) A geiant-local edge index** — a table (or columns) persisting v4 edge-records addressable
     by `target` fingerprint, plus a write path, and a store-backed `seek` over it. Owns the data;
     needs a migration (0006-style) and an ingestion path.
   - **(c) `seek` over grafomem's enforcement index via HTTP** — matches this spec's "the enforcement
     index is the query surface" (§3) and mirrors the existing injected `httpFetchProofs`; adds a
     network dependency and a cross-service contract.
2. **Is v4 issuance gated on (1)?** Recommended posture: issuance of validity-affecting edges
   (`revokes`/`supersedes`) MUST NOT begin until at least one identity-holding consumer can enforce
   them — otherwise the edge is signed but binds nowhere. `continues`-only issuance (lineage, not
   validity) is not gated by this, since it degrades rather than enforces.
