---
status: accepted
decision_date: 2026-08-31
record_date: 2026-08-31
provenance: promoted-from-working-draft (internal CGR delta spec, draft-0.2, 2026-08-27); resolved at P0.4 under 0004
scope: cgr.attestation.v3 → cgr.attestation.v4
---

# 0001 — Grounding dimension: true-additive vs schema-bump

- **Status:** **Accepted** 2026-08-31 (P0.4) — resolved to **Option B (schema-bump)**: grounding
  fields ship in `cgr.attestation.v4`. The shared versioning decision is
  [0004](0004-no-identity-continuity-across-rotation.md).
- **Record date:** 2026-08-31

## Context

A proposed **`grounding` dimension** for CGR — judgment calls of the form "this claim is grounded in
these memory entries," resolved later by audit, scored by the existing Beta(1,1) pipeline, attested
by the existing schema — introduces a small number of **new signed-body fields** (an oracle identity
and an audit-policy digest, plus an optional uncertainty-mass count). Adding fields to the signed
body forces a wire-format choice.

Whether those fields can be carried under the **existing** schema version rather than a **new** one
is **empirically constrained**, and the constraint is **re-derivable from the public verifier
source**: the deployed verifiers (e.g. `@gns-foundation/cgr-verify`) verify over the whole
non-envelope signed body and gate acceptance only on the schema string — so additive fields verify,
but a new schema string does not until verifiers widen their accepted set. The design question below
is what remains.

**Prior art:** the internal working draft *"Grounding-Audit Outcome Type — CGR Delta Spec"*
(draft-0.2, 2026-08-27), which specifies the dimension and its resolution semantics. That draft is
the authoritative technical content; this record promotes only its open wire-format question and
otherwise stands alone.

## Decision — resolved at P0.4 (2026-08-31)

**Resolved: Option B (schema-bump).** Grounding's new signed fields ship in a new
`cgr.attestation.v4` schema string, not additive under `v3`. This follows the shared versioning
decision in [0004](0004-no-identity-continuity-across-rotation.md): the relation edge is
validity-affecting, so old verifiers must fail closed rather than silently ignore — and grounding
rides the same bump. The options, as considered:

How to ship the new signed-body fields:

- **Option A — True-additive** (keep the current `cgr.attestation.v3` schema string; add the fields):
  minimal ecosystem friction. It carries a **semantics-safety concern** — a relying party could
  misread the attestation's scope — that must be mitigated before adoption.
- **Option B — Schema-bump** (new schema string): consumers without grounding semantics **fail
  closed** on grounding attestations — arguably the correct behavior — at the price of ecosystem
  friction (every verifier must widen its accepted-schema set first, expand-contract).

The trade is **semantics-safety vs. ecosystem-friction** — a deliberate call, not a default.
*(Picked at P0.4, 2026-08-31: Option B, via a `v4` bump — see the resolution above.)*

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
4. Relationship to [0004](0004-no-identity-continuity-across-rotation.md) — a **third instance**.
   0004 argues the shared principle is that the schema has **no general mechanism for expressing a
   relation between attestations** (supersedes / continues / corrects), and that 0001, 0002 and 0004
   are three faces of that one absent primitive rather than three missing fields. **Whoever picks up
   this record should read 0004 first**: resolving 0001 in isolation risks solving one third of the
   problem and guaranteeing a fourth instance.
