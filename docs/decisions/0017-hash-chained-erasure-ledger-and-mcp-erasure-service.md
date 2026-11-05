# 0017 — Hash-chained erasure ledger; a real erasure service in the MCP process

**Status:** proposed 2026-11-04 (approved for build by the operator 2026-11-04) · **Relates to:**
[0013](0013-erasure-certificate-issuance-requires-ledger-entry.md) (ledger before certificate),
[0014](0014-erasure-coverage-status-vocabulary.md) (coverage vocabulary),
[0016](0016-erasure-certificates-are-factual.md) (factual certificates), and the LangGraph adapter's
`receipt_log.py` (the chain design this transplants). Item 2 of the erasure-honesty audit.

## Question

0013 guarantees a certificate never exists without a prior ledger row. It does not guarantee the row
is still there. `erasure_ledger` had no sequence and no hash: append-only was enforced only by role
grants (`grafomem_rt` SELECT-only, 010), restore-scrub ordered rows by `created_at` — a timestamp —
and a row dropped or reordered by a higher privilege, or by restoring an older backup of the ledger
itself, was undetectable. The adapter solved exactly this in 1.1.0.

Separately, the MCP process (`grafomem serve --mcp stdio|sse`) was built from a `backend_factory`
alone. `delete_memory` carried a certificate branch gated on `backend.db_url` that no real backend
has (dead outside tests — deliberately left in place by 0016 as item 1b), and `verify_erasure` tried
an unauthenticated GET to a hard-coded `https://cloud.grafomem.com/...`, so it could never return a
verdict and always answered `ERASED_UNVERIFIED`. It also called `backend.get(ref)` before deleting,
a method no real backend had until 0016 — so on real backends the tool was raising.

## Decision

### A. Chain the ledger, additively; seal the legacy rows behind one genesis row

Migration **021** adds `seq BIGINT` (NOT NULL, UNIQUE after backfill), `prev_hash TEXT`,
`entry_hash TEXT`. No existing column of any existing row is rewritten; the migration's one UPDATE
fills the new `seq` on legacy rows and runs as the migrate role. No `CREATE TABLE`, so the split-role
grant rule is not triggered; the ledger role's SELECT+INSERT from 009 covers the new columns.

**Construction is byte-identical to the adapter** so one verifier reads both logs:
`entry_hash = sha256(prev_hash || canonical(payload))`, canonical JSON = sorted keys, no whitespace,
`GENESIS = "0"*64`. The payload is the row's content columns (`entry_id, tenant_id, entry_type,
fact_ref, content_hash, certificate, backfill, created_at`); `created_at` is set by the writer and
canonicalised to UTC ISO-8601 with microseconds so it hashes identically on the way in and out.
Payload values must be JSONB-stable (str/int/bool/None/nested) because the verifier rebuilds the
payload from the row as Postgres returns it; certificate payloads are, and a test pins it.

**Legacy segment.** Rows that predate 021 get `seq` by `ROW_NUMBER() OVER (ORDER BY created_at,
entry_id)` and keep NULL hashes. One sealing row, `entry_type = 'chain_genesis'`, `tenant_id =
'__ledger__'`, `prev_hash = GENESIS`, `entry_hash = sha256(GENESIS || canonical({"entry_type":
"chain_genesis","legacy_entry_ids":[…ids in seq order…]}))`, is computed **in SQL** inside the
migration (PG ≥ 11 `sha256()`; CI pg15, compose pg16) and recomputed by the Python verifier from the
legacy ids — their membership and order are what the seal commits to. *Found while building:* the
idempotency guard must be `HAVING NOT EXISTS`, not `WHERE` — with aggregates a WHERE filters the
input rows and the aggregate still emits one (empty) row, inserting a second, wrong genesis. The
existing restore-scrub tests caught it; the migration applied twice now adds nothing.

**Write path** (`ErasureLedger._append`), one transaction: `pg_advisory_xact_lock(hashtext(
'erasure_ledger_chain'))` → read the head → explicit `seq+1`, `prev_hash`, `entry_hash`, `created_at`
→ INSERT. The lock serialises writers across processes and pool connections; explicit `seq` means a
rolled-back insert leaves no gap, so contiguity is a verifiable property. A **completely empty**
ledger self-seals with `genesis([])` on first write (INSERT only — works under the ledger role in
cloud; there is no legacy segment to misrepresent). Rows present but unsealed **refuse** writes: that
state needs 021's backfill.

**Verifier.** `verify_ledger(rows) -> (ok, problems)`: seq contiguous from 1, exactly one genesis,
genesis hash recomputes from the legacy ids, every later row links and recomputes; each problem
names its `seq`. Exposed as `GET /v1/erasure/ledger/verify` → `{ok, rows, problems}` under
`erasure:execute` — the only erasure scope that exists; the draft's `erasure:read` was invented, and a
read-only scope is a separate decision. The route is declared before `/{certificate_id}` so "ledger"
is never captured as an id. **restore-scrub verifies first**, carries `ledger_chain_ok` /
`ledger_chain_problems` in its stats, and **still replays** what is present — refusing would leave
revived data un-scrubbed. It skips `chain_genesis` explicitly.

**Self-host parity.** `ErasureLedger.ensure_schema()` executes the shipped 021 file itself (one
source, cannot drift) and also converges the `backfill` column that 009 had and `ensure_schema()`
never created. Cloud never runs it (migration-owned, same gate as `app.py`).

**Release step.** Production's `GRAFOMEM_LEDGER_URL` is the ledger *role* on the main `railway`
database, so 021 is reached by the ordinary release migration — no second `--url` run.

### B. The MCP process gets the same erasure service the HTTP server has

`serve --mcp` builds `ErasureLedger` + `ErasureProofService` from the same configuration `app.py`
uses (Postgres backend, `ERASURE_SIGNING_KEY`/`GRAFOMEM_SIGNING_KEY`, `GRAFOMEM_LEDGER_URL` with the
same fallback; schema ensured only where `boot_migrations_enabled` says the HTTP server would) and
passes it as `create_mcp_server(backend_factory, erasure_service=…)`. Then:

- `delete_memory`: the dead `db_url` branch is removed (1b retired). The content observed in the
  tool's own pre-delete read is passed as `fact_content`, so the certificate is factual (0016).
  Outcomes are stated: `certificate_status = issued | not_issued: fact_still_present | not_issued:
  <ExceptionName> | not_issued: no erasure service in this MCP process`. Nothing is faked.
- `verify_erasure`: the signature is verified **in-process** (`verify_certificate`, same key
  material). `GRAFOMEM_VERIFY_URL` remains an optional additional remote check that can only make the
  verdict worse (`tamper=True`), never better. The hard-coded cloud URL is gone. `sealed_probe` stays
  `None` (0016); `ERASED_VERIFIED` still requires `reverify` + a real probe.
- No decision trail in the MCP process → that subsystem is recorded `unverified` (0014).

## Consequences

- The ledger can now prove a row was removed or reordered after the fact; it still cannot restore
  it. External anchoring of the chain head (gcrumbs epochs) is the natural follow-up.
- `GET /v1/erasure/ledger/verify` reports chain integrity across all tenants; it exposes no tenant
  content.
- `reapply_ledger()` now returns two extra keys; callers that typed it as `dict[str, int]` see
  `dict[str, Any]`.
- `create_mcp_server`, `run_mcp_stdio`, `run_mcp_sse` gain an optional `erasure_service` argument;
  existing callers are unaffected.
- Out of scope: per-subject crypto-erasure (item 3, product decision); the tenant-destruction
  certificate's placeholder body; a read-only erasure scope.
- Non-vacuity: `tests/test_erasure_ledger_chain.py` (verifier, real-Postgres chain incl. concurrent
  writers, superuser row deletion, legacy sealing, migration idempotency against the shipped file,
  JSONB round-trip, the split-role gate) and the rewritten `tests/test_mcp_governance.py`; each
  guard proven by flipping it (link check, head chaining, the HAVING guard, in-process tamper).
