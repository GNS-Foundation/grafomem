# Decision records — GNS-Foundation / GRAFOMEM

Decision records for the **standard**: the CGR attestation schema (`cgr.attestation.v3` and
successors), the verification recipe, the conformance suite, and the governance choices that
shape them — **and for the reference implementations that exercise the standard**, where those
implementations reveal something about the standard's expressive limits. This is the
Foundation's record of *why the standard is the way it is*.

Scope note: records here concern the **standard and its reference implementations**. A record
belongs here when the finding is about what the schema can or cannot express — even when it
surfaced in an implementation (e.g. GEIANT delegation certificates in
[0003](0003-principal-identity-is-not-stable.md) and
[0004](0004-no-identity-continuity-across-rotation.md)). **Commercial/product decisions still
live in their own repos**; where the two touch, they cross-reference.

## Records

| # | Title | Status |
|---|---|---|
| [0001](0001-cgr-grounding-dimension-additive-vs-schema-bump.md) | Grounding dimension: true-additive vs schema-bump | **accepted** 2026-08-31 (P0.4 → v4 bump) |
| [0002](0002-cgr-governance-domain-and-backfill.md) | Governance domain + backfill expression | **accepted** 2026-08-31 (P0.4 → v4 bump) |
| [0003](0003-principal-identity-is-not-stable.md) | Principal identity is not stable | **accepted** 2026-08-31 |
| [0004](0004-no-identity-continuity-across-rotation.md) | No identity-continuity across rotation | **accepted** 2026-08-31 (P0.4 → generic relation edge, v4 bump) |
| [0005](0005-custody-managed-principals.md) | Custody-managed principals (target design) | proposed — unblocked (0004 resolved) |
| [0006](0006-enforcement-boundary-for-revocation.md) | Enforcement boundary: what revocation guarantees, and what lies outside it | proposed — raised from geiant #11/#12 |

**Resolved at P0.4 (2026-08-31):** 0001, 0002 and 0004 shared one versioning question — how CGR adds
signed meaning — and it resolved to a **schema bump to `cgr.attestation.v4`**, carrying a generic
relation edge (0004) plus the grounding (0001) and governance/backfill (0002) fields. 0005 is now
**unblocked** (its own adoption is a separate decision). The `v4` schema design, migration, and
golden-fixture regeneration are P1. *(Formal acceptance lands when this PR merges.)*

## Convention

- **One numbered record per decision or open question**, `NNNN-short-slug.md`, starting at
  `0001`. Numbers are permanent; a superseded record is not deleted — a later record supersedes
  it and both link.
- **YAML front matter** on every record:

  ```yaml
  ---
  status:        proposed | accepted | superseded-by-NNNN | deprecated
  decision_date: YYYY-MM-DD   # when the decision was made; omit/"—" while still proposed
  record_date:   YYYY-MM-DD   # when this record was written
  provenance:    <how it came to be recorded — e.g. promoted-from-working-draft, backfilled, relocated-from-<repo>>
  scope:         <the schema/spec version(s) affected — e.g. cgr.attestation.v3>
  ---
  ```

  `decision_date` and `record_date` are **distinct on purpose**: a record written after the fact
  must not imply it was captured when the decision was made.
- **Body sections:** `## Context` · `## Decision` · `## Consequences` · `## Open questions`.
  A record whose `status: proposed` states the question and options under **Decision** but does
  **not** pick one — it is resolved by a later edit that flips `status` to `accepted` with a
  `decision_date`, or by a superseding record.

This format is deliberately legible alongside the ADR-style records used in product repos, but it
is the Foundation's own convention, not an import: records here are numeric (`0001`), product ADRs
are `ADR-000N`, and the two cross-link.
