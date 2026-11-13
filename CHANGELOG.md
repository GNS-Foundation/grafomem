# Changelog

All notable changes to the `grafomem` package (the server, backends, conformance suite and
MCP server — `src/aml`). The LangGraph checkpointer (`langgraph-checkpoint-grafomem`) is
released separately and has its own changelog under `adapters/`.

Decision records referenced below live in `docs/decisions/`.

## [Unreleased]

Data-subject scope and per-data-subject crypto-erasure (ADR-0019; GMP v0.2 §7.7). Additive on
the protocol (a twelfth, optional capability); the Cloud part needs migration 022 and is
opt-in per tenant. A minor bump is warranted when released.

### Fixed
- **MCP over SSE dropped the connection after every message** (0.5.0 and earlier): both
  Starlette route endpoints in `run_mcp_sse` returned `None`, so Starlette raised
  `TypeError: 'NoneType' object is not callable` per message, uvicorn closed the connection
  and Python MCP clients hung on their next message (`httpx.ReadError`, "Error in
  post_writer"). Routes now follow the SDK's `FastMCP.sse_app()` shape: the SSE endpoint
  returns a `Response`, the message handler is mounted as an ASGI app at `/messages/`
  (clients learn the path from the `endpoint` event). Reported from the Valmerra demo behind
  agentgateway 1.6 (2026-11-13). Regression test `tests/test_mcp_sse_transport.py`: three
  sessions × two calls complete, no `TypeError` logged; it hangs against the old routes.
  stdio was never affected.

### Added
- **`DATA_SUBJECT` capability** (GMP §7.7): `WriteOptions.data_subject_id`,
  `Memory.data_subject_id`, `erase_data_subject(id, opts) -> [ref]` — two-sided on the read
  path, idempotent, superseded versions erased with the head. SQLite (reference) and Postgres
  claim it; HNSW and the GMP reference backend raise `CapabilityNotSupported` when the option is
  set. Conformance gains a two-direction `DATA_SUBJECT` row (leak / over-erasure).
- **Per-data-subject keys (Cloud, Postgres):** random DEK per data subject wrapped under the
  tenant DEK (`data_subject_deks`), ciphertext prefixed `gfm.ds1:`; opt-in per tenant
  (`POST /v1/erasure/data-subject-encryption`). Rows written before the opt-in stay
  tenant-keyed.
- **`POST /v1/erasure/data-subject`**: erases every fact of a data subject, deletes their
  embeddings synchronously (no sweeper window), destroys their key, issues one certificate
  per fact with `coverage.data_subject_key = absent` and the key fingerprint bound (or
  `unverified` for facts that were never key-covered), and appends a
  `data_subject_key_destroyed` ledger entry. The response and the ledger carry fingerprints,
  never the identifier.
- **`GET /v1/erasure/{id}/verify`** checks a data-subject-key claim against the key store's
  tombstone: a restored key fails verification even with a valid signature; the response
  gains `data_subject_key`.
- **Restore-scrub** replays `data_subject_key_destroyed` entries and re-destroys keys a
  restore revived (`data_subject_keys_redestroyed` in its stats).
- **MCP:** `erase_data_subject` tool; `write_memory` accepts `data_subject_id`.
- **REST:** `options.data_subject_id` on write; `data_subject_id` on memory responses.

### Changed
- **Wrapped DEKs are read and written on the ledger URL** (`GRAFOMEM_LEDGER_URL`), not the
  primary `db_url` — the key manager's docstring had claimed a separate store that was not
  the case. Migration 022 grants `grafomem_ledger` its DML on `tenant_deks` /
  `data_subject_deks`; the runtime role keeps its `tenant_deks` DML until a later held
  migration (deploy-window safety).

### Added (ADR-0020, part 1)
- **`grafomem signer`** — a ledger signer process: holds an Ed25519 receipt key and the only
  write handle to a chained JSONL receipt log; `POST /v1/sign/receipt`, `GET
  /v1/sign/{health,entries,verify}`, bearer-token auth (`GRAFOMEM_SIGNER_TOKEN`, required
  unless `UNSAFE_LOCAL_DEV`). Entry format identical to the LangGraph adapter's receipt log,
  so its `verify_log` verifies the signer's entries unchanged. Callers: the adapter's
  `RemoteReceiptSigner` (1.3.0); the API in part 2.

### Migration
- **022** (additive, idempotent): `memories.data_subject_id` + index, `data_subject_deks`,
  `tenants.data_subject_encryption` (default false), role grants. Applied by the pre-deploy
  runner; needs operator approval before it runs on a deployed verifier. `ensure_schema()` in
  self-host executes the same file.

## [0.5.0] — 2026-11-06

Three honesty fixes to the erasure path and the MCP surface, each with a decision record.
Behaviour changes are listed under **Breaking** — read them before upgrading.

### Breaking
- **`grafomem serve --mcp` requires `--tenant <id>` (or `GRAFOMEM_MCP_TENANT`)** with the
  shipped backends. An MCP process now serves exactly one tenant; a backend that declares
  `MULTI_TENANT` (SQLite as well as Postgres) refuses to start unbound. (ADR-0018)
- **`POST /v1/erasure/issue` returns 409** when the primary store still holds the fact: no
  erasure certificate is minted for a fact that was not erased. The request fields
  `signing_key` (caller-supplied signing key) and `memory_deleted` (accepted and ignored) are
  removed; `memory_deleted` is still tolerated from older SDKs. (ADR-0016)
- **`run_conformance` over MCP** runs on an isolated scratch store of the same backend kind
  (SQLite: a temp file) and **returns an error on Postgres** until a scratch-schema strategy
  exists — it no longer writes ~1,170 synthetic facts into the serving store. Its response
  gains `directions`, `workload`, `evidence.store` and drops the never-populated
  `bootstrap_ci`, `ops_until_gone`, `isolation_conformance`. (ADR-0018)
- **Regulatory report findings can now be `PARTIAL` / `INSUFFICIENT_DATA`.** GDPR Art. 17/25
  and DORA Art. 9 were hard-coded `COMPLIANT`; they are computed from the erasure service's
  stats, signing configuration and the tenant's *active* policy types, with an
  `evidence_basis` (observed vs declared) and a note that governance policies are evaluated
  via `/v1/governance/evaluate`, not inline on memory operations. (ADR-0016)

### Added
- **Hash-chained erasure ledger** (migration `021`, additive): `seq` / `prev_hash` /
  `entry_hash`; rows that predate the migration are sealed behind one `chain_genesis` row
  whose hash commits to their ordered ids. Writers chain under a Postgres advisory lock.
  Construction is byte-identical to the LangGraph adapter's receipt log. `GET
  /v1/erasure/ledger/verify` → `{ok, rows, problems}`; restore-scrub verifies first and
  reports `ledger_chain_ok` / `ledger_chain_problems`. (ADR-0017)
- **Server-observed content digest on the delete endpoint**: `POST /v1/stores/{id}/delete`
  reads the record before deleting, so `fact_content_hash` is a digest the server computed —
  previously `null`. New optional backend method `get(ref) -> Memory | None` on SQLite,
  Postgres and HNSW (the POINT_LOOKUP sibling of `exists()`). (ADR-0016)
- **A real erasure service in the MCP process**: `serve --mcp` builds the same
  `ErasureLedger` + `ErasureProofService` as the HTTP server when Postgres, a signing key and
  `GRAFOMEM_LEDGER_URL` are configured. `delete_memory` issues factual certificates and
  states the outcome in `certificate_status`; `verify_erasure` verifies signatures
  in-process. (ADR-0017)
- `CHANGELOG.md` (this file). `aml.__version__` now derives from the installed package
  metadata instead of a literal that had drifted to `0.1.3`.

### Fixed
- `delete_memory` / `verify_erasure` over MCP raised `AttributeError: 'get'` on every real
  backend (the only test ran on a mock that had `get()`); real-backend tests added.
- MCP `verify_erasure` fabricated a `sealed_probe` from a certificate's mere existence and
  tried an unauthenticated GET to a hard-coded cloud URL, so it could never return a verdict.
- `run_conformance` over MCP crashed on every tested capability (`.metrics` / `.value` do not
  exist on `CapabilityResult`) and returned `PASS` for undeclared, untested capabilities;
  `get_capabilities(verify=true)` swallowed the crash into `UNTESTED`. Verdicts now come from
  `CapabilityResult.passed`; `NOT_DECLARED` / `UNTESTED` / `ERROR:` are reported as such.
- Conformance "evidence" hashes were hashes of the seed; they are now digests over the results
  (`rollup_hash`) and the corpus lock (`corpus_hash`, `null` when absent).
- `coverage_gaps` in the certificate's governance record was a hard-coded `[]`; it is derived
  from the coverage probe.
- In MCP stdio mode the banner (and status lines) were written to stdout, the JSON-RPC
  channel; everything human-readable now goes to stderr.
- The SDK's `erasure.issue()` docstring claimed the call deleted the fact; it never did.

### Migration
- Apply migration `021_erasure_ledger_chain.sql` (the ordinary pre-deploy release step covers
  it; it is additive and idempotent). After it runs, `GET /v1/erasure/ledger/verify` should
  report `ok: true` with the pre-existing rows sealed behind one genesis row.

## [0.4.2] and earlier
See the git history and `docs/decisions/`.
