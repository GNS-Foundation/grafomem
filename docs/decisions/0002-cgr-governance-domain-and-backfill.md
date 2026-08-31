---
status: proposed
decision_date: "—"
record_date: 2026-08-31
provenance: relocated-from eu-governed-agent/ADR-0007 (surfaced 2026-08-31 attempting to capture backfilled governance decisions)
scope: cgr.attestation.v3
---

# 0002 — Governance domain + backfill expression (capture gaps)

- **Status:** **Proposed** (open — do not treat as decided)
- **Record date:** 2026-08-31
- **Origin:** surfaced in the EU governed-agent project when attempting to attest six
  **backfilled** governance decisions (decided 2026-08-29, recorded 2026-08-31) to GRAFOMEM Cloud
  via `grafomem-cgr`. The attempt was **not possible**; the two blockers are standard-level schema
  questions, so they are recorded here. The product-side stub is
  `~/eu-governed-agent/docs/decisions/ADR-0007`.

## Context

`cgr.attestation.v3` (and the `grafomem-cgr` capture path over it) cannot express what a
governance / backfilled decision record requires. Two distinct gaps.

### Gap (a) — no governance/strategy domain

The capture `domain` is an **enum locked to `{deploy-verification, security-scan,
adversarial-review}`** — dev-loop capability domains. There is **no `governance` / `strategy` /
`compliance` value**, so business/legal/GTM decisions have no truthful domain to be filed under.
Forcing them into a dev domain (even with `verifiability_tag: rule` so they don't move a score)
would **misattribute** them and pollute the reputation substrate.

### Gap (b) — no way to express backfill

A `cgr.attestation` records **`created_at` = capture time** only. There is **no distinct
`decision_date`** and **no `backfilled` / provenance flag**. A decision made 2026-08-29 and
recorded 2026-08-31 is, in the record, indistinguishable from one captured the moment it was made —
the record would silently imply **contemporaneous capture**, which is false.

## Decision (open — two schema questions, not resolved here)

1. **Domain vocabulary** — does CGR add a `governance`/`strategy` domain (or a domain taxonomy that
   isn't a fixed dev-loop enum), and how is it kept from diluting score semantics (governance
   records should be recordable but **non-scoring**, cf. `verifiability_tag: rule`)?
2. **Backfill / temporal provenance** — does the signed body gain a distinct `decision_date` + a
   `backfilled`/`recorded_at` provenance, so "when decided" ≠ "when recorded" is **first-class and
   tamper-evident**, not stuffed into free-text?

This is the **same true-additive-vs-schema-bump tradeoff** as
[0001](0001-cgr-grounding-dimension-additive-vs-schema-bump.md): the new fields
(`governance` domain value, `decision_date`, `backfilled`) can be added true-additive under the
unchanged `cgr.attestation.v3` schema string (deployed verifiers accept unknown signed-body fields —
probe 2026-08-28), or via a schema bump. **Not resolved here** — it should resolve together with
0001, under one principle.

## Consequences

- Until resolved, **governance/backfilled decisions cannot be attested** honestly via `grafomem-cgr`
  — the EU governed-agent decisions remain recorded only in signed git, `capture_status: not-attested`.
- A `governance` domain plus `verifiability_tag: rule` would let such decisions be **recorded on the
  tamper-evident chain without scoring** an agent — the desired behavior.
- Backfill fields, once signed-body, make decision-vs-record lag **verifiable**, not asserted.

## Product requirement — not only a dogfooding issue

Gap (b) is a **product requirement**, not merely an internal capture inconvenience. In an **AML
disposition**, *when the analyst decided* vs *when the decision was recorded* is a **regulatory
distinction a supervisor will ask about** (timeliness of SAR/STR filing, decision-vs-record lag,
audit reconstruction). A compliance product whose attestations cannot separate decision-time from
record-time is not supervisable on that axis. (Flagged product-side for B0.7 in the EU
governed-agent ADR-0007.)

## Open questions

- Do domain and backfill ship together or separately, and under which path (A/B of 0001)?
- Governance domain: a fixed extended enum, or an open domain taxonomy with a conformance-marked
  vocabulary?
- Does `decision_date` belong in the **signed body** (tamper-evident, the strong form) or the
  envelope (advisory)? The regulatory use in the product requirement argues for the signed body.
