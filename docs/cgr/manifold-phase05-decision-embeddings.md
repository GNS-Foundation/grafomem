# Manifold Phase-0.5 — embed capability-relevant decision content

The prerequisite that gives CGR real geometry: a 384-d embedding of each CGR-attributed decision's
**capability content**, stored in a vault-only `decision_embeddings` table keyed by
`(tenant_id, decision_id)` — joinable `decision→outcome→domain`. **No scoring change.**

## What is embedded (capability signal vs PII/noise)
`DecisionTrailService.capability_text(query, raw_output, params)` composes:
- **include:** decision-type, `verifiability_tag`, dimension (`cgr_schema`), `tool`, `reason_code`
  (structured tags) + the situational `query` context + the `raw_output` rationale — the signal that
  separates paid/default.
- **exclude (noise/identity):** the pseudonymized `invoice_ref`/`invoice_id` (high-entropy join key),
  `agent_key`/`agent_handle` (identity — would cluster geometry by agent, not capability). The exact
  tokens are also string-stripped from the composed text (belt-and-suspenders).
- **PII:** a built-in redactor (emails/phones) runs as **defense-in-depth**; the real control is the
  vault posture below. Names may remain in the *text*, but the text is never persisted — only the vector.

## Storage — `decision_embeddings` (vault, `ops/decision_embeddings.sql`)
`(tenant_id, decision_id, embedding vector(384), tokenizer_id, created_at, valid_from, valid_until,
erasure_pending)`, PK `(tenant_id, decision_id)`, FK→`decision_records(decision_id) ON DELETE CASCADE`.
**Vault posture (encrypted-tier PII-derived — matches `memory_embeddings`):** RLS ENABLE+FORCE +
`tenant_isolation` policy, DML granted **only** to `grafomem_rt`, **never serialized by any API**,
erasure-swept (FK cascade for hard-delete + `erasure_pending` + the erasure sweeper). **Applied as
superuser** (owner ≠ grafomem_rt so FORCE enforces for the app role) — not the auto-migration.

## Write path — `DecisionTrailService.log` hook
Single choke point (covers all mint sites), gated on `cgr_schema`, **best-effort/fail-open**: an embed
failure (missing table, model load, RLS) never blocks the governed decision — it is caught, metered
(`grafomem_decision_embeddings_total{result}`), and dropped. Runs only when an embedder is injected
(prod wiring shares the one memoized BGE model; None elsewhere ⇒ no-op).

## Backfill — `ops/backfill_decision_embeddings.py`
RLS-aware (sets `app.current_tenant`), idempotent (`ON CONFLICT DO NOTHING` + pre-scan of embedded
ids), **scan-guarded** (aborts if a per-tenant scan falls below its Step-0 floor — the invoice_ref
lesson). Decrypts `query_enc`/`raw_output_enc` per-tenant DEK (falls back to plaintext columns in
dev/test), composes the identical `capability_text`, embeds, stores the vector only.

## Tests
`tests/test_decision_embeddings.py`: capability-text exclusions; hook writes vector-only + fail-open +
no-op-without-embedder + skips-non-CGR; **acceptance** (synthetic disjoint-vocab paid/default separation
> +0.06 — the plumbing gate, not a claim on the real n=5); **vault-only** (source scan: no route/exporter
references the table); **no-plaintext-leak** (no content column; planted token absent from the row);
backfill idempotency + scan-guard. RLS fail-closed coverage: `decision_embeddings` added to
`tests/test_rls_decision_hitl.py` REAL_TABLES.

## Deploy
branch → PR → CI → **Cowork reviews migration + write-path + vault-only + no-plaintext-leak** → apply
`ops/decision_embeddings.sql` as superuser → deploy **through the governed dev loop → Camilo attest**
→ run the backfill.

## Phase-2 ROADMAP (log, do not build yet)
A **purpose-built capability descriptor** beats raw-content embedding: instead of embedding the raw
decision text (whose geometry is diluted by templated tokens + residual PII and needs redaction),
derive an explicit, PII-free capability feature vector (structured decision features + a learned or
curated descriptor) and embed that. Better signal-to-noise, no decrypt-at-embed, no inversion risk.
Deferred — Phase-0.5 ships raw-content-embedding-into-vault as the pragmatic prerequisite.
