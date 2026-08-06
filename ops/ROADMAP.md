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

## Encrypt decision-record context (PII) — MUST-FIX before Mauricio

**Signal.** `OrchestratorService.propose_action` records governed decisions via
`decision_trail.log` WITHOUT passing encryption (matching `demo_routes._record_and_sign`), so
the decision `query`/context — which holds real prospect **company + person names** — is stored
**plaintext** in `decision_records` on the corp tenant. Provider keys and governed memory are
encrypted at rest (EnvIdentity/Fernet), so this is an inconsistency and a PII exposure.

**Roadmap.** Pass `encryption=self._encryption` in `propose_action` (verify CGR still reads
`agent_key`/`invoice_ref` from `parameters`, which are separate from the encrypted query — see
the #13 CGR+encryption fix), and re-encrypt the 21 rows already written for
`gtm-outreach-agent@ulissy`. Consider the same for `demo_routes._record_and_sign`. Tracked as a
session chip; parallel to the loop PRs — does not block the PR-4/5/6 review checkpoint.
