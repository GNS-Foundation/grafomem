# Ulissy dogfooding — roadmap items surfaced by Phase 0

Two items logged per the charter's "failure signal → roadmap" discipline. Both are real
limitations Phase 0 worked around with semantic mapping; neither blocks the live loop.

## B. Generic decision / outcome types (platform)

**Signal.** The governed schema is invoice-shaped: decisions carry `invoice_id`, outcomes
are drawn from `{paid, default, disputed, late, written_off}`. Phase 0 maps GTM onto these
(`meeting_booked → paid`, `passed → default`) — fine for a binary landed/missed outreach,
but there is **no faithful representation for interim engagement** (`replied`, `opened`),
so those states emit no outcome and don't inform the score. The same wall hits the next
fronts: a finance certifier or a code agent don't have "invoices" or "paid/default".

**Roadmap.** Add first-class `decision_type` / `outcome_type` to the governed model so any
front (GTM, finance, code, research) is native, with per-type outcome vocabularies (e.g.
GTM: `booked | engaged | passed | no_response`). Schema + migration + endpoint change.
Do it in **Phase 2**, when the Finance/Code fronts arrive and force the generalization —
not before, so Phase 0 ships a live loop now.

**Where it bites today:** `ops/ingest_front.py` `STATUS_TO_OUTCOME` / `INTERIM_STATUSES`.

## Verified-contact source (GTM front)

**Signal.** The GTM front needs verified contacts (emails/titles) to populate the ledger's
decide columns at any scale. **Apollo is on the Free plan — its search + enrichment APIs
are blocked**, so target discovery/enrichment can't run programmatically.

**Roadmap.** Either a paid Apollo seat (search + enrichment APIs) or an alternative
verified-contact provider. Owned by the GTM front (Cowork), gates Phase 1 throughput.
Until resolved, the ledger's decide columns are populated by hand.

## No tenant / test-data reset (purge) path (platform)

**Signal.** The governed substrate is append-only by design (outcomes are latest-wins,
decisions never dedup or delete). There is no supported way to purge a tenant's test/
synthetic data or delete a tenant. Phase 0 seeded the Ulissy tenant with 6 synthetic
sample rows (with fabricated `meeting_booked`/`passed` outcomes) before real data existed;
those records — and their contribution to the CGR posterior — are now permanent on that
tenant. The only clean way to an honest zero-state start was to provision a *fresh* tenant
and orphan the seeded one.

**Roadmap.** An admin-scoped, audited purge/reset (or a first-class "sandbox → promote"
tenant lifecycle) so a tenant can be started clean without abandoning it. Until then, the
operational rule is: **never seed a tenant you intend to keep with synthetic outcomes** —
prove the loop on a throwaway tenant, provision the keeper empty.

## Enforce RLS on the HITL tables — MUST-FIX before multi-tenant

**Signal.** RLS is not enforced on `hitl_approval_requests` / `hitl_approvers`. The live smoke
found **two** app-layer tenant-scoping bugs in `hitl_routes.py` in one pass: `list_requests`
filtered by `tenant_id=None` (require_scope returns None), and `verify_request` had no tenant
filter at all (cross-tenant IDOR leaking signature + `context_bytes`/recipient PII). Both fixed
at the app layer, but there is no DB backstop.

**Roadmap.** Enforce Postgres RLS on the HITL tables the way `memories` is protected (#12/#12a:
`app.current_tenant` + FORCE ROW LEVEL SECURITY under `grafomem_rt`; `tests/test_cgr_rls.py` is
the proving pattern), so an app-layer scoping bug can't leak cross-tenant by construction.
Prioritize before onboarding a 2nd tenant. Tracked as a session chip.

## Encrypt decision-record context (PII) — MUST-FIX before Mauricio — ADDRESSED (branch phase2/encrypt-decision-context)

**Signal.** `OrchestratorService.propose_action` recorded governed decisions via
`decision_trail.log` WITHOUT passing encryption, so the decision `query`/context — which holds
real prospect **company + person names** — was stored **plaintext** in `decision_records` on the
corp tenant. Provider keys and governed memory are encrypted at rest (EnvIdentity/Fernet), so this
was an inconsistency and a PII exposure.

**Fix (class-wide audit of every governed decision_trail.log writer):**
- `propose_action` (orchestrator.py) — **FIXED**: now passes `encryption=self._encryption` (the
  prod TenantKeyManager). This is the path that wrote the corp GTM rows.
- `execute_step` ×2 (orchestrator.py) — already correct (both passed encryption). No change.
- `demo_routes._record_and_sign` (`/v1/governed/decisions`, `/verify-batch`) — **FIXED**: threads
  `encryption` from `request.app.state.encryption` (safe helper `_tenant_encryption`, None in tests).
- `landing` / `world_model` — **EXEMPT**: never call `decision_trail.log` (verified). Their
  outcome/review writes go through the GMP `memories` store, already encrypted (#13).
- `execution_receipts.issue_receipt` — **EXEMPT**: persists BLAKE2b **hashes** of input/output,
  never the plaintext context.

CGR is provably unaffected: `load_substrate`/`join_decisions_to_outcomes` read decisions ONLY from
`parameters` (JSONB, never encrypted); `_row_to_record` decrypts keyed on `query_enc` presence
(the #13-correct pattern). Tests: `tests/test_decision_context_encryption.py` (no plaintext PII at
rest, CGR join survives, migration idempotent).

**Migration.** `ops/encrypt_decision_context.py` — idempotent, reuses each tenant's existing DEK;
`--dry-run` (step-0 count/sample), `--verify` (zero plaintext decision_records + plaintext
llm_providers heuristic), `--all-gtm` (all 3 GTM tenants). Runs INSIDE Railway (private DB +
`GRAFOMEM_MASTER_KEY`). Gated: run against prod only AFTER Cowork adversarial review of diff + script.

**LIVE-VERIFIED SCOPE (2026-08-07, Railway console).** The brief's "~21 corp rows" was wrong. Real
state: `decision_records` holds **424 rows, 383 plaintext**. The `gtm-outreach-agent@ulissy` PII
(real prospect company names, e.g. "Abound (Fintern)") is **61 plaintext rows across 3 tenants** —
corp `5605470c` (34), machine `600e0890` (21, = the brief's "~21"), orphaned `e1c5e06` (6). Decision
(Camilo): migrate all 3 (no tenant-purge path ⇒ machine-tenant PII persists otherwise).

**Follow-ups (NOT in this branch):**
- **Systemic plaintext (~322 non-GTM rows).** 383 total plaintext − 61 GTM = ~322 rows from other
  agents/demo/legacy paths (only 41 rows encrypted table-wide). Needs its OWN characterization
  (which store_ids/tenants/paths; live PII vs dev/test) before any backfill. Do NOT fold into the
  GTM migration.
- `decision_routes` `/v1/decisions/log` — generic public decision API, same omission, but its GET
  routes return `query` un-decrypted, so encrypting writes there needs a **coordinated write+read**
  change (API contract). Deferred as its own PR.
