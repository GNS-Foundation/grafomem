---
status: proposed
decision_date: "—"
record_date: 2026-08-31
provenance: promoted-from-working-draft (internal CGR delta spec, draft-0.2, 2026-08-27)
scope: cgr.attestation.v3
---

# 0001 — Grounding dimension: true-additive vs schema-bump

- **Status:** **Proposed** (unresolved — a deliberate call, not a default)
- **Record date:** 2026-08-31

## Context

A proposed **`grounding` dimension** for CGR — judgment calls of the form "this claim is grounded in
these memory entries," resolved later by audit, scored by the existing Beta(1,1) pipeline, attested
by the existing schema — introduces a small number of **new signed-body fields** (an oracle identity
and an audit-policy digest, plus an optional uncertainty-mass count). Adding fields to the signed
body forces a wire-format choice.

Whether those fields can be carried under the **existing** schema version rather than a **new** one
is **empirically constrained** — see the internal probe (2026-08-28) for the constraint. The design
question below is what remains.

**Prior art:** the internal working draft *"Grounding-Audit Outcome Type — CGR Delta Spec"*
(draft-0.2, 2026-08-27), which specifies the dimension and its resolution semantics. That draft is
the authoritative technical content; this record promotes only its open wire-format question and
otherwise stands alone.

## Decision (open — two options, not resolved here)

How to ship the new signed-body fields:

- **Option A — True-additive** (keep the current `cgr.attestation.v3` schema string; add the fields):
  minimal ecosystem friction. It carries a **semantics-safety concern** — a relying party could
  misread the attestation's scope — that must be mitigated before adoption.
- **Option B — Schema-bump** (new schema string): consumers without grounding semantics **fail
  closed** on grounding attestations — arguably the correct behavior — at the price of ecosystem
  friction (every verifier must widen its accepted-schema set first, expand-contract).

The trade is **semantics-safety vs. ecosystem-friction** — a deliberate call, not a default.
**This record does not pick one.**

## Consequences

- **Either path** requires regenerating the byte-parity **golden fixture** (the wire-format lock).
- **Option A** makes the mitigation a prerequisite: the semantics-safety concern must be closed
  (in the verify recipe and docs) before additive fields are adopted.
- **Option B** is a coordinated ecosystem change (`@gns-foundation/cgr-verify`, `@geiant/core`, the
  read surface) that must land before issuance emits the new schema.
- Per-dimension Beta(1,1) is **never pooled across dimensions**, independent of the choice.

## Open questions

1. **The choice itself** (A vs B) — owner + criteria.
2. Carried from the draft (unresolved): epoch/oracle-change policy; adjudicator key management
   before the first contested refutation; pre-registered audit sampling fraction.
3. Relationship to [0002](0002-cgr-governance-domain-and-backfill.md) — a **second instance** of the
   same tradeoff, on different fields. One principle should resolve both.
