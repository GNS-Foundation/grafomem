# ops/ — Ulissy as tenant #1 of Grafomem Cloud (Phase 0)

Stands up **Ulissy** as its own internal tenant on production Grafomem and runs the
charter's **decide → resolve → score** loop for the GTM/outreach front, reusing the
governed substrate we already ship (no new data plane). Each front-agent becomes a
scored actor; each outreach decision a governed decision; each resolution an outcome;
CGR scores our own agent.

**Design decision:** Phase 0 uses **semantic mapping (A)** onto the existing
invoice-shaped governed fields — zero schema change. Generic decision/outcome types are
[ROADMAP.md](ROADMAP.md) item B (Phase 2).

## Guardrail (non-negotiable)

This adapter **only records** decisions/outcomes. It has **no send path** — it never
emails or contacts anyone. Every outreach to a named human stays a founder-approved
edge action, handled out of band.

## Files

| file | what |
|---|---|
| `setup_ulissy_tenant.py` | Provision the prod Ulissy tenant, pin a stable `agent_key`, verify isolation. Idempotent. |
| `ingest_front.py` | Ingest a GTM ledger (CSV/JSON) → governed decisions + outcomes + reviews. Idempotent, re-runnable. |
| `common.py` | Base URL + creds helpers (mirrors `demo/common.py`, scoped to Ulissy). |
| `sample_ledger.csv` | Sample fixture proving the loop end-to-end until Cowork drops in the real export. |
| `.ulissy_creds.json` | **gitignored** — `{tenant_id, api_key, agents{...}}`. Written by setup; 0600. |
| `.ulissy_ingest_state.json` | **gitignored** — manifest of posted decision refs (client-side idempotency). |

## Quickstart

```bash
# 1. Provision the tenant (writes gitignored creds, pins the agent_key, checks isolation)
GRAFOMEM_BASE=https://grafomem-production.up.railway.app python ops/setup_ulissy_tenant.py

# 2. Preview the mapping without writing anything
python ops/ingest_front.py ops/sample_ledger.csv --dry-run

# 3. Ingest (safe to re-run — decisions dedup on a local manifest; outcomes/reviews are server-idempotent)
python ops/ingest_front.py ops/sample_ledger.csv

# 4. Read the live CGR tier for the front-agent
curl -s -H "X-API-Key: $(python -c 'import json;print(json.load(open("ops/.ulissy_creds.json"))["api_key"])')" \
  https://grafomem-production.up.railway.app/v1/cgr/scores | python -m json.tool
```

## Ledger input contract (what Cowork exports)

CSV (header row) or JSON (`[{...}]`, or `{"rows": [...]}`). One row per outreach call.
Column names are case-sensitive; unknown columns are ignored.

| column | required | maps to | notes |
|---|---|---|---|
| `ref` | no | decision `invoice_id` / outcome+review `invoice_ref` | If blank, derived as `OUT-<company>-<person>`. Must be stable across re-exports. |
| `company` | if no `ref` | decision `context.company` | |
| `person` | if no `ref` | decision `context.person` | A named human ⇒ the send is a founder edge action (Phase 0 doesn't send). |
| `channel` | no | `context.channel` | e.g. email, linkedin |
| `message_variant` | no | `context.message_variant` | which message variant was used (`variant` also accepted) |
| `rationale` | no | decision `reason` + context | why this outreach (`reason` also accepted) |
| `status` | no | **outcome** | resolve column — see mapping below |
| `edge_approved` | no | `context.edge_approved` | founder approved the send? (yes/no/true/false) |
| `agent_tier` | no | decision `agent_tier` | optional TierGate snapshot; else the pinned default |
| `reviewer` | no | review `reviewer_handle` | founder edge-approval is the first reviewer signal (`reviewer_handle` also accepted) |
| `rating` | with `reviewer` | review `rating` | 0.0–1.0 (clamped) |
| `resolved_date` | no | outcome `outcome_date` | ISO date (`outcome_date` also accepted) |

### status → outcome (semantic mapping A)

* **paid** (landed): `meeting_booked`, `meeting`, `booked`, `call_booked`
* **default** (missed): `passed`, `no_response`, `bounced`, `declined`, `unsubscribed`, `not_interested`
* **interim — no outcome yet**: `proposed`, `sent`, `queued`, `opened`, `clicked`, `replied`, `pending`, blank

`replied`/`opened` are interim on purpose: the invoice-shaped outcome set has no
"engaged" member, so recording one would distort the score. Faithfully representing
engagement needs generic outcome types — [ROADMAP.md](ROADMAP.md) item B. Any status
that is neither a known outcome nor a known interim state is reported as an **unknown
status** at ingest time (catches ledger typos instead of silently dropping them).

## The scored subject

`gtm-outreach-agent@ulissy` — carried on each decision as `agent_handle` (label) plus a
**stable** `agent_key` (the CGR grouping key, pinned once by `setup_ulissy_tenant.py`).
Structure it so `finance-agent@ulissy`, `code-agent@ulissy`, `research-agent@ulissy`
slot in later under the same tenant.

## Dashboard

Point the existing cloud-v2 `/reputation` Command Center at the Ulissy tenant by logging
in with the Ulissy key (it reads `/v1/cgr/scores` with `X-API-Key`) — no code change for
Phase 0. `gtm-outreach-agent@ulissy` appears ranked by CGR tier.

## Weekly refresh

`.github/workflows/ulissy-weekly-refresh.yml` re-runs the resolve+score pass weekly.
It needs two repo secrets (set by a human — never commit a key):
`GRAFOMEM_BASE` and `ULISSY_API_KEY`.
