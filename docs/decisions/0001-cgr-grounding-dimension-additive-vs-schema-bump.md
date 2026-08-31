---
status: proposed
decision_date: "—"
record_date: 2026-08-31
provenance: promoted-from-working-draft (claude/grounding-audit-outcome-spec.md, draft-0.2, revised 2026-08-27; unknown-field probe 2026-08-28)
scope: cgr.attestation.v3
---

# 0001 — Grounding dimension: true-additive vs schema-bump

- **Status:** **Proposed** (unresolved — a deliberate call, not a default)
- **Record date:** 2026-08-31
- **Prior art:** [`claude/grounding-audit-outcome-spec.md`](../../claude/grounding-audit-outcome-spec.md)
  — *Grounding-Audit Outcome Type — CGR Delta Spec, draft-0.2*, revised against CGR paper draft
  v0.1 (2026-08-27) and the cgr-verify unknown-field probe (2026-08-28). That working draft is the
  authoritative technical content; this record promotes its one **open schema question** into the
  Foundation decision log without rewriting its findings. The draft stays where it is.

## Context

The draft adds a **`grounding` dimension** to CGR — judgment calls of the form "this claim is
grounded in these memory entries," resolved later by audit, scored by the existing Beta(1,1)
pipeline, attested by the existing schema. It reuses the resolution machinery unchanged; the delta
collapses to **two genuinely new signed-body fields** — `oracle_id` and `audit_policy` (plus an
optional `n_unresolvable`) — taking v3's signed body from **18 → 20–21 fields**.

The **unknown-field probe (2026-08-28)** established the constraint empirically (cross-language,
deployed verifiers, throwaway key): deployed verifiers **tolerate unknown signed-body fields** — so
the additive path is open — **but the only known-field check anywhere is `schema ∈ ACCEPTED_SCHEMAS`.**
Therefore the additive path is open **only if grounding attestations keep the `cgr.attestation.v3`
schema string**; minting as `cgr.attestation.v3.1` is rejected by every verifier in the wild at the
schema check. That leaves one design decision, which the draft explicitly parked (its §5, open item
§11.2) for this record.

## Decision (open — two options, not resolved here)

**How to ship the grounding dimension's new fields:**

- **Option A — True-additive** (same `cgr.attestation.v3` string, new fields): zero ecosystem
  friction (every deployed verifier already recanonicalizes the whole non-envelope body and accepts
  unknown fields). **Risk:** a consumer that checks signature + schema and never reads `dimension`
  **silently takes a grounding score for a capability-reputation score** — semantic conflation, the
  one failure a scope-honest system shouldn't tolerate quietly. **Mitigation (draft §5):** `dimension`
  becomes a **MUST-read** in the verify recipe and docs.
- **Option B — Schema-bump** (new schema string): old verifiers **fail closed** on grounding
  attestations — arguably *correct* for consumers without grounding semantics — at the price of
  ecosystem friction (every verifier must widen its `ACCEPTED_SCHEMAS`).

The trade is **semantics-safety vs. ecosystem-friction**. Per the draft: "deliberate call, not a
default." **This record does not pick one.**

## Consequences

- **Either path** requires regenerating the byte-parity **golden fixture** (the wire-format lock).
- **Option A** makes `dimension` load-bearing for correctness — it must be elevated from
  informational to a required verifier check, and documented as such, or the conflation risk is
  live.
- **Option B** is a coordinated ecosystem change: `@gns-foundation/cgr-verify`, `@geiant/core`, and
  the read surface must accept the new schema before issuance emits it (expand-contract), or
  consumers reject valid grounding attestations.
- Per-dimension Beta(1,1) is **never pooled across dimensions** (draft §5) — receivables reliability
  must not launder grounding failures or vice-versa — regardless of which path is chosen.

## Open questions

1. **The choice itself** (A vs B) — owner + criteria; the draft leaves it explicitly open.
2. From the draft's §11 (unresolved there, carried here): epoch/oracle-change policy (what bumps the
   grounding calibration epoch, and who signs the bump); adjudicator key management for the human
   channel before the first contested `refuted`; pre-registered sampling fraction for the
   re-verification channel.
3. Relationship to [0002](0002-cgr-governance-domain-and-backfill.md) — a **second instance** of the
   same true-additive-vs-schema-bump tradeoff, on different fields (governance domain + backfill).
   Whatever principle resolves the choice here should resolve it there.
