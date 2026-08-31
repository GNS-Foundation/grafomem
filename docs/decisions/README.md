# Decision records — GNS-Foundation / GRAFOMEM

Decision records for the **standard**: the CGR attestation schema (`cgr.attestation.v3` and
successors), the verification recipe, the conformance suite, and the governance choices that
shape them. This is the Foundation's record of *why the standard is the way it is* — distinct
from product decisions, which live with the products (per the standard↔products split).

Scope note: records here concern the **standard**. Commercial/product decisions live in their own
repos; where the two touch, they cross-reference.

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

## Index

| # | Title | Status | Scope |
|---|---|---|---|
| [0001](0001-cgr-grounding-dimension-additive-vs-schema-bump.md) | Grounding dimension: true-additive vs schema-bump | **Proposed** | `cgr.attestation.v3` |
| [0002](0002-cgr-governance-domain-and-backfill.md) | Governance domain + backfill expression (capture gaps) | **Proposed** | `cgr.attestation.v3` |
