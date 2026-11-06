# Changelog

All notable changes to the `grafomem` package (the server, backends, conformance suite and
MCP server — `src/aml`). The LangGraph checkpointer (`langgraph-checkpoint-grafomem`) is
released separately and has its own changelog under `adapters/`.

Decision records referenced below live in `docs/decisions/`.

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
