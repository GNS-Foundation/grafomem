---
status: proposed
decision_date: "—"
provenance: contemporaneous
capture_status: not-attested
---

# cgr.cosign.v1 profile registry + the `cgr.disposition.v1` entry (Proposed)

- **Status:** **Proposed** — slug-titled, **no number reserved** (assigned on acceptance; the
  cross-repo numbering caution applies — see the decisions README). Accept by **merging this PR**.
- **Relates to:** `docs/cgr/cgr-cosign-v1-spec.md` (§8 profile resolution; §11 Q3 registry
  format/governance, OPEN), decisions [0009](0009-standard-expresses-one-actor-approval-needs-two.md)
  (the two-party envelope), [0010](0010-cosign-predicate-unresolved-must-reject.md),
  [0011](0011-cosign-issuer-trust-and-uniform-unresolved.md); eu-governed-agent B2
  (`docs/specs/b2-aml-disposition-evidence-schema.md`) which **binds to** this entry; and
  eu-governed-agent ADR-0008 (a gap the product needs is fixed in the ledger/standard, upstream —
  which is why this is a grafomem/standard record, not a product-side registry).

## Context

`cgr.cosign.v1` profile resolution (§8 step 2) is normative, but the **registry document was unwritten**
(§11 Q3) — the conformance corpus (`conformance/cgr-cosign-v1/`) shipped a *fixture* and said the real
registry must match it or retarget. `cgr.disposition.v1` is the **first profile** of the envelope and
the record eu-governed-agent B2 binds to; its runtime record class + attest route
(`conformance/cgr-disposition-v1/`, PR #173) cannot resolve a profile that does not exist. This record
writes the registry and its first entry, via the **standard** rather than the product.

## Decision

1. **The normative registry lives at `docs/cgr/cosign-profile-registry.json`** — the document
   `verify()` step 2 resolves against. This partially closes §11 Q3 (location + governance:
   closed-per-version, extended by a decision record per entry).
2. **The `cgr.disposition.v1` entry:**
   - `approval_mode`: **`bound`**.
   - `approver_signature`: **`REQUIRED`**, **required-ness unconditional** (no predicate) — a
     disposition without an approver signature is always rejected; no content-conditional exemption.
   - **Assurance tier is SURFACED, never gated** — a record's assurance marker (e.g. `none`,
     `bank_vouched`, `qes`) is accepted and surfaced by the verifier/read path; it MUST NOT be a basis
     to reject, and a low/`none` tier MUST NOT be silently upgraded or made mistakable for a vouched one.
   - **`content_body` is profile-defined and opaque** to the envelope and the grafomem runtime; the AML
     content profile lives **downstream** in eu-governed-agent B2 (ADR-0008 — nothing AML-specific
     upstream).

## Consequences

- The `conformance/cgr-disposition-v1/` corpus's "fixture, not the contract" markers are **removed** in
  this PR: `cgr.disposition.v1` now mirrors this ratified entry. (A separate `cgr.disposition.v1.predicate`
  profile remains in the corpus **only** as a verifier-conformance fixture exercising
  `predicate_unresolved`; it is NOT part of the disposition contract, whose required-ness is unconditional.)
- The runtime route (PR #173) MUST resolve profiles from this registry, not from any corpus fixture.
- Adding a future profile or changing this entry is a new decision record + a registry edit (per-version,
  supersede-don't-restate).
- **Scope limit (PR #173 runtime slice) — no unified `decision_trail` entry.** A recorded disposition
  writes **only** the append-only, tenant-scoped, counter-signed `cosign_dispositions` row (migration
  016); **that row IS the disposition's governed trail.** The runtime deliberately does **not** also
  write a `decision_trail` entry for this slice: `DecisionTrail.log` is inference-shaped
  (query/model_id/raw_output) and mapping a two-party HITL disposition onto it would fabricate
  misleading fields. A future unified trail entry is a separate decision (it needs a disposition→trail
  field mapping); until then, read dispositions from `cosign_dispositions` / `GET /v1/dispositions/{id}`.

## Open sub-questions

- §11 Q3's "closed-per-version vs open" governance is only **partially** settled here (location + a
  per-entry decision-record process); a fuller registry-governance record may follow if more profiles land.
