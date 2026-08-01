# CGR-v1 Substrate Instrumentation Spec — Kapwork POC

*First executable CGR homework (from `gns-cgr-integration.md`): instrument the Kapwork GRAFOMEM POC so it accumulates the CGR substrate **from the first invoice**. Reference implementation validated on Kapwork-shaped synthetic data: `cgr_substrate.py` (deterministic). Drafted Aug 1, 2026. Canonical copy synced into the repo at `docs/cgr/cgr-substrate-instrumentation-spec.md`; source of truth is the "Beyond Orchestration" claude.ai Project.*

> **The one rule:** *capture now, score later.* CGR scoring can come later, but the data it needs must be logged from invoice #1 — because the ground-truth labels (paid/default) and the attribution keys are **irreversible if missed.** This spec is mostly about *capture discipline*, not scoring.

## 1. The three irreversible fields (get these on invoice #1)
1. **`invoice_ref` — stable business join key.** The paid/default outcome arrives weeks-to-months later from a different system; without a stable key the calibration label is lost forever. The single most important field.
2. **`agent_handle` — stable GEIANT identity (+ version).** Format `invoice-certifier@kapwork-receivables` (+ `model_id`/`prompt_hash`). Reputation attaches to a stable referent.
3. **`verifiability_tag` — `rule` vs `judgment`.** Hard rule (amount>PO, missing approval, duplicate = verifiable) vs judgment (fraud smell, concentration risk = unverifiable). Separates the calibration slice (verifiable) from the value slice (unverifiable) — the entire "verify the reviewer" mechanism depends on it. ~31% rule / ~69% judgment in the reference.

## 2. The capture schema (three event types)
**A. DecisionEvent** — at decision time; extends `POST /v1/governed/decisions`. Fields: `decision_id`, `agent_handle` (+model_id, prompt_hash), `agent_tier` (GEIANT TierGate snapshot = capability prior), `invoice_ref`, `amount`/`po_amount`/`approval_present`/`duplicate`, `decision` (certify|reject), `reason_code` (amount>PO | no_debtor_approval | duplicate | risk_judgment | clean), `reason_text`, `verifiability_tag` (rule|judgment), `agent_confidence`, `ts`.

**B. OutcomeEvent** — async ground-truth label; stored as a GMP Fact:
```
Fact(predicate="receivable_outcome", subject=invoice_ref,
     object="paid"|"default"|"disputed"|"late"|"written_off", valid_from=outcome_date)
+ days_to_outcome, amount_recovered, source
```
Bi-temporal + supersession handle revisions natively (late→default, clawback after paid). Append-only, auditable.

**C. ReviewEvent** *(optional but high-value)* — a funder/analyst rating of a certification; enables the "verify the reviewer" bridge. Stored as a Fact:
```
Fact(predicate="certification_review", subject=decision_id,
     object=rating[0,1], valid_from=ts)  + reviewer_handle (GEIANT)
```
*(Implementation note, Ticket #3: `subject=invoice_ref` is used instead of `decision_id`, because reviewer calibration joins reviews→outcomes on `invoice_ref`; `decision_id` is preserved in metadata. Reviews are many-per-invoice — dedup/revision key is `(invoice_ref, reviewer_handle)`.)*

## 3. GRAFOMEM mapping (no new data plane — reuse what's live)
- **Decisions** → existing governed-decision path (`decision_records` + `execution_receipts`), CGR fields added to `parameters` JSONB. Already signed + gcrumbs-chained.
- **Outcomes & reviews** → GMP Facts in dedicated stores (`cgr-outcomes`, `cgr-reviews`), keyed by `invoice_ref`. Bi-temporal memory + supersession + audit for free.
- **The join** → `invoice_ref` links decisions ↔ outcomes ↔ reviews; `agent_handle` attributes to a GEIANT identity.
- **Capability prior** → snapshot GEIANT TierGate tier + GRAFOMEM conformance (M8) at decision time (`agent_tier`).
- **The one new thing to build:** an outcome-ingestion endpoint (even a manual CSV importer) so labels are never dropped. (Ticket #1 done; review-ingestion is Ticket #3.)

## 4. CGR-v1 computation (defined now; run later)
Per agent, over `judgment`-tagged certifications: Beta prior from tier (k≈4); each resolved outcome updates Beta full-weight; reviewer-weighted early signal (calibrate reviewer Brier on resolved invoices → weight → apply to unresolved). Output a CGR tier that feeds GEIANT TierGate (the capability-grounded upgrade, not a separate score).

**Success metric:** CGR predicts an agent's certified-portfolio default rate and beats the naive baseline. Reference: corr −0.99 (full) / −0.54 (early, 25% resolved). Productionized: `src/aml/cgr/` — live path (tier=None) −1.000, tier-wired + evidence-gated ceiling −0.986.

## 5. Non-goals
- No live CGR scoring in the POC UI — just capture (scoring is an offline pass; now also `GET /v1/cgr/scores`).
- No cross-domain transfer (ρ≈0.3 → receivables only).
- No public CGR publication — substrate stays private (spill≈0 moat guard).
- No governance-policy enforcement tied to CGR yet.

## 6. Definition of done (homework)
1. Every certification writes a DecisionEvent with the three irreversible fields + `agent_tier`. *(Ticket #1 ✓)*
2. An outcome-ingestion pipe writes OutcomeEvent Facts keyed by `invoice_ref`. *(Ticket #1 ✓)*
3. Review capture available. *(Ticket #3)*
4. A join query reconstructs per agent: certifications → outcomes → reviews, with capability tier as-of decision. *(Ticket #2 `load_substrate` ✓; reviews Ticket #3)*
5. The offline CGR-v1 pass runs on accumulated real data and reports the success metric. *(engine + validate ✓ on synthetic; awaits real data)*

## Ties to
`gns-cgr-integration.md`, `reputation-score-design.md`, `contracts-vs-reputation.md`, `reconstruction-beta-results.md` (keep the substrate private).
