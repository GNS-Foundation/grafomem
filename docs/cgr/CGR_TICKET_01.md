# Claude Code Ticket #1 — CGR Substrate Capture (Grafomem)

**Repo:** `~/grafomem`  ·  **Owner (architect):** Camilo + Cowork-chat (spec)  ·  **You:** implementer
**Scope:** *Substrate capture only.* Instrument the existing Kapwork governed-decision path so it accumulates the CGR substrate from the first invoice. **No scoring, no UI, no DB migration if avoidable.**

> **Context (why):** CGR = the capability-grounded reputation layer that will upgrade GEIANT's TierGate. Before any scoring, the Kapwork POC must *capture* what CGR needs, because the ground-truth labels (invoice paid/default) arrive weeks/months later and are lost forever if not linkable. The scoring algorithm already exists as a validated reference — `cgr_substrate.py` (ask Camilo; do NOT reimplement it here). This ticket only makes the data land correctly.

## Read these first (real files, do not guess)
- `src/aml/cloud/demo_routes.py` — the Kapwork surface: `GovernedDecisionRequest`, `VerifyBatchRequest`, `_record_and_sign(...)`, `create_governed_router(...)`, `/v1/governed/decisions`, `/v1/governed/verify-batch`.
- `src/aml/cloud/verification.py` — the deterministic rules engine (`evaluate_invoice`) = the **verifiable / `rule`** layer.
- `src/aml/cloud/decision_trail.py` — `DecisionTrailService.log(..., parameters: dict|None, ...)`. `parameters` is JSONB → **add CGR fields here, no migration.**
- `src/aml/server/stores.py` — `StoreManager` (`create`, `get`, `get_or_404`, `get_default`).
- `src/aml/backends/interface.py` — `MemoryBackend.write(content, options)`, `WriteOptions` (has `valid_from`, `tenant_id`, `metadata`), `supersede(...)` if present.
- `src/aml/wire.py` — the `Fact` quadruple (predicate/subject/object/valid_from + importance/seq/superseded_by/tenant_id).
- `tests/` — match the existing test style/fixtures.

## The three irreversible fields (must be on every governed decision)
1. **`invoice_ref`** — the stable join key. *Already present* as `invoice_id`. Task: guarantee it is always persisted into the decision `parameters`, and log a WARNING when it is null (a decision with no join key can never receive an outcome).
2. **`agent_handle`** — GEIANT-style stable agent identity, e.g. `invoice-certifier@kapwork-receivables`. *Missing.* Add it.
3. **`verifiability_tag`** — `"rule"` | `"judgment"`. *Missing.* Add it. Rules-engine decisions (`verify-batch`) = `"rule"`; agent-posted judgment calls (`/governed/decisions`) default `"judgment"`.

## Task A — extend the request models & persistence (`demo_routes.py`)
1. Add to **`GovernedDecisionRequest`**: `agent_handle: str = "invoice-certifier@kapwork-receivables"`, `verifiability_tag: str = "judgment"`, `agent_tier: float | None = None` (optional GEIANT TierGate snapshot; leave None for now).
2. Add to **`VerifyBatchRequest`**: `agent_handle: str = "invoice-rules-engine@kapwork-receivables"`, `agent_tier: float | None = None`. (verify-batch decisions are always `verifiability_tag="rule"` — set it internally, not a request field.)
3. In **`_record_and_sign(...)`**, extend the `parameters=` dict passed to `decision_trail.log(...)` to include:
   ```python
   parameters={
       "invoice_id": invoice_id,          # existing
       "invoice_ref": invoice_id,         # explicit CGR join key (alias, keep both)
       "decision": decision,              # existing
       "reason_code": reason_code,        # NEW: pass a structured code (see Task B)
       "agent_handle": agent_handle,      # NEW
       "verifiability_tag": verifiability_tag,  # NEW
       "agent_tier": agent_tier,          # NEW (nullable)
       "cgr_schema": "cgr.decision.v1",   # NEW: version tag for the substrate
   }
   ```
   Thread `agent_handle`, `verifiability_tag`, `agent_tier`, `reason_code` through `_record_and_sign`'s signature. If `invoice_ref` is None → `logger.warning("CGR: governed decision recorded with no invoice_ref — will be unjoinable to outcome")`.
4. In **`/v1/governed/decisions`** and **`/v1/governed/verify-batch`** handlers, pass the new values through (`verify_batch` hard-codes `verifiability_tag="rule"`).

## Task B — structured reason codes (`verification.py`)
`evaluate_invoice` currently returns `(decision, reason_text)`. Add a **stable `reason_code`** alongside the human text so CGR can group calls without NLP. Return `(decision, reason_code, reason_text)` (update callers), with codes:
`amount_exceeds_po` · `amount_or_po_missing` · `no_debtor_approval` · `duplicate` · `clean`.
(These are all `rule`/verifiable. A future `risk_judgment` code will come from a judgment agent via `/governed/decisions` — not this ticket.)

## Task C — the outcome-ingestion pipe (NEW endpoint)
The paid/default label arrives later from the funder/receivables ledger. Add an append-only intake.

- **Endpoint:** `POST /v1/governed/outcomes` (same router/auth as governed decisions; tenant-scoped).
- **Body:**
  ```python
  class OutcomeEvent(BaseModel):
      invoice_ref: str
      outcome: str            # "paid" | "default" | "disputed" | "late" | "written_off"
      outcome_date: str | None = None      # ISO; default = now
      amount_recovered: float | None = None
      source: str = "manual"               # e.g. "funder_feed", "kapwork_ledger", "manual"
  ```
- **Storage:** write an **append-only GMP record** in a dedicated store `store_id="cgr-outcomes"` (create-if-missing via `StoreManager`). Use the real write API you find in `interface.py`/`stores.py` — represent the outcome as a `Fact`-shaped record:
  `predicate="receivable_outcome"`, `subject=invoice_ref`, `object=outcome`, `valid_from=outcome_date`, `tenant_id=<tenant>`, `metadata={amount_recovered, source, cgr_schema:"cgr.outcome.v1"}`.
- **Revisions:** if an outcome for the same `invoice_ref` already exists and differs (e.g. `late`→`default`, or a clawback after `paid`), record the new one and **supersede** the prior (`superseded_by`) if the backend supports it; never hard-delete (append-only, auditable).
- Return `{invoice_ref, outcome, recorded_at, superseded_prior: bool}`.
- Optional convenience: `POST /v1/governed/outcomes/bulk` accepting a list (for CSV imports on day one).

## Task D — substrate export / join (for the offline CGR-v1 pass)
Add one read path so the validated `cgr_substrate.py` logic can run on real data later:
- **Endpoint:** `GET /v1/cgr/substrate/export` (tenant-scoped, `require_scope(request, "decisions:read")`).
- **Returns** joined rows, one per governed decision, shaped to match `cgr_substrate.py`'s expected fields:
  ```json
  {"decisions":[{"decision_id","invoice_ref","agent_handle","agent_tier",
     "decision","reason_code","verifiability_tag","created_at",
     "outcome": "paid|default|null", "outcome_date": "…|null"}], "count": N}
  ```
  Build it by reading `decision_records` for the tenant (pull the CGR fields out of `parameters`) and left-joining the `cgr-outcomes` store on `invoice_ref` (latest, non-superseded outcome wins). Keep it simple/paginated; correctness over performance.

## Tests (match `tests/` style)
- Decision persists all three irreversible fields + `reason_code` + `cgr_schema` in `parameters` (assert via `decision_trail` read).
- `verify-batch` tags every decision `verifiability_tag="rule"` and emits structured `reason_code`s.
- Missing `invoice_ref` logs the warning (assert on caplog).
- `POST /v1/governed/outcomes` writes a retrievable outcome; a second differing outcome for the same `invoice_ref` supersedes (old one flagged, not deleted).
- `GET /v1/cgr/substrate/export` returns decisions with correctly joined outcomes (paid/default) and nulls for unresolved.

## Acceptance / definition of done
1. A scripted flow works end-to-end: run a `verify-batch` of sample invoices → `POST /v1/governed/outcomes` for a few of them → `GET /v1/cgr/substrate/export` returns rows where those invoices show their outcome and the rest show `null`.
2. Every governed decision carries `invoice_ref`, `agent_handle`, `verifiability_tag` (+ `cgr_schema`).
3. Outcomes are append-only and supersede on revision.
4. New tests green; existing suite (`pytest tests/`) still green.
5. No DB migration (fields live in `parameters` JSONB; outcomes in a GMP store). If you believe a migration/columns are warranted for query performance, STOP and flag it to Camilo rather than adding one.

## Non-goals (do NOT do in this ticket)
- No CGR scoring/Beta engine (that's the next ticket — the `src/aml/cgr/` module reading this export).
- No portal/Command-Center UI.
- No cross-domain / multi-vertical anything (receivables only).
- No publishing of scores; keep the substrate private.
- No changes to the signing/gcrumbs receipt logic — only the recorded `parameters` + a new outcomes store + two read/write endpoints.

## Hand-off
When done, produce: the diff summary (files touched + new endpoints), the test output, and a 3-line note on any deviation from this ticket. Camilo will bring the diff back to the Cowork chat for architecture review against `docs/cgr/cgr-substrate-instrumentation-spec.md`.
