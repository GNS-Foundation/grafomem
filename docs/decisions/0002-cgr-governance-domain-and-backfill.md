---
status: proposed
decision_date: "—"
record_date: 2026-08-31
provenance: surfaced during a backfilled governance-decision capture attempt (2026-08-31)
scope: cgr.attestation.v3
---

# 0002 — Governance domain + backfill expression (capture gaps)

- **Status:** **Proposed** (open — do not treat as decided)
- **Record date:** 2026-08-31

## Context

Attempting to attest **backfilled governance decisions** (a decision made earlier and recorded
later) via the `grafomem-cgr` capture path over `cgr.attestation.v3` is not currently possible. Two
distinct schema gaps.

### Gap (a) — no governance/strategy domain

The capture `domain` is an **enum locked to `{deploy-verification, security-scan,
adversarial-review}`** — dev-loop capability domains. There is **no `governance` / `strategy` /
`compliance` value**, so a governance decision has no truthful domain to be filed under. Forcing it
into a dev domain (even with `verifiability_tag: rule` so it does not move a score) would
**misattribute** it and pollute the reputation substrate.

### Gap (b) — no way to express backfill

A `cgr.attestation` records **`created_at` = capture time** only. There is **no distinct
`decision_date`** and **no `backfilled` / provenance flag**. A decision made on one date and recorded
on a later one is, in the record, indistinguishable from one captured at decision time — the record
silently implies **contemporaneous capture**, which is false.

## Decision (open — two schema questions, not resolved here)

1. **Domain vocabulary** — does CGR add a `governance`/`strategy` domain (or a domain taxonomy that
   isn't a fixed dev-loop enum), and how is it kept from diluting score semantics (governance records
   recordable but **non-scoring**, cf. `verifiability_tag: rule`)?
2. **Backfill / temporal provenance** — does the signed body gain a distinct `decision_date` + a
   `backfilled`/`recorded_at` provenance, so "when decided" ≠ "when recorded" is **first-class and
   tamper-evident**, not stuffed into free-text?

This is the **same true-additive-vs-schema-bump tradeoff** as
[0001](0001-cgr-grounding-dimension-additive-vs-schema-bump.md): the new fields can be carried under
the unchanged `cgr.attestation.v3` schema string, or via a schema bump. **Not resolved here** — it
should resolve together with 0001, under one principle.

## Requirement — temporal provenance is first-class

Gap (b) is not merely an internal capture convenience. **Temporal provenance — the distinction
between decision-time and record-time — is a first-class requirement for regulated and audited use
cases, because it is a distinction a supervisor may examine.** An attestation format that cannot
separate *when a decision was made* from *when it was recorded* is insufficient for those uses. This
record states the **schema requirement**; which use cases need it is out of scope here.

## Consequences

- Until resolved, backfilled/governance decisions **cannot be attested honestly** via `grafomem-cgr`;
  they remain in signed source control, unattested.
- A `governance` domain plus `verifiability_tag: rule` would allow recording on the tamper-evident
  chain **without scoring** an agent — the desired behavior.
- A signed-body `decision_date` makes decision-vs-record lag **verifiable**, not asserted.

## Open questions

- Do domain and backfill ship together or separately, and under which path (A/B of 0001)?
- Governance domain: a fixed extended enum, or an open domain taxonomy with a conformance-marked
  vocabulary?
- Does `decision_date` belong in the **signed body** (tamper-evident) or the envelope (advisory)?
  The temporal-provenance requirement argues for the signed body.
