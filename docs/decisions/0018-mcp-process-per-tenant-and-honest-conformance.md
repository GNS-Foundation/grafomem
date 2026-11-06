# 0018 — MCP: one process, one tenant; conformance on a scratch store; verdicts from the engine

**Status:** proposed 2026-11-06 (approved for build by the operator 2026-11-06) · **Relates to:**
[0016](0016-erasure-certificates-are-factual.md), [0017](0017-hash-chained-erasure-ledger-and-mcp-erasure-service.md)
(the MCP erasure path), the conformance suite (`aml.eval.conformance`), and the MCP/agentgateway audit of
2026-11-05 (findings 1, 2, 4, 9, 10, 11, 12, 13, 14 — reproduced by running, not by reading).

## Question

An end-to-end test of the MCP server behind agentgateway found that the surface GRAFOMEM exposes to agents
contradicted GRAFOMEM's own discipline in five ways, each reproduced against `grafomem==0.4.2`:

1. **Untenanted.** `write_memory`/`retrieve_memories` carried no tenant; `delete_memory` took it from the
   fact's *metadata* and `verify_erasure` from a caller *argument*. Two JWT identities on one store: one read
   the other's secret word for word. The backend declared `MULTI_TENANT` the whole time — "declared ≠
   behaviour", in our own MCP surface.
2. **`run_conformance` wrote ~1,170 synthetic facts into the serving store** per call, and they then came
   back from real retrievals.
3. **It crashed on every capability it actually tested** (`CapabilityResult` has `directions`/`point`, not
   `metrics`/`value`) — and the tests passed, because they mocked the engine to return the wrong shape.
4. **It returned `PASS` for capabilities that were neither declared nor tested**, from `leakage=0.0,
   recall=1.0` defaults; `get_capabilities(verify=true)` swallowed the crash into `UNTESTED`.
5. **"Evidence" fields were hashes of the seed**, and the stdio banner was written to stdout — the JSON-RPC
   channel. (0017 then added five more stdout lines; corrected here.)

Also: `delete_memory` called `backend.get()` before 0016 added it — the tool raised on every real backend —
and the test that should have caught it ran on a mock that had `get()`.

## Decision

### One MCP process serves exactly one tenant

`create_mcp_server(..., tenant_id=…)`; `grafomem serve --mcp … --tenant <id>` (env `GRAFOMEM_MCP_TENANT`).
Every write, retrieve, list, delete and verify is scoped to the bound tenant through `WriteOptions`/
`RetrieveOptions`/`scoped_audit`. A ref owned by another tenant is **not found** to this process — never
deleted, never acknowledged, never "present". A caller-supplied `tenant` argument is accepted only if it
names the bound tenant; otherwise `tenant_mismatch`. A backend that declares `MULTI_TENANT` **refuses to
serve unbound** (fail closed, at the CLI *and* in `create_mcp_server`). Both shipped backends — SQLite as
well as Postgres — declare it, so both require a bound tenant; the assumption that SQLite could run unbound
was wrong and is recorded here.

*Why this and not gateway-injected identity:* the audit showed agentgateway's "one target per tenant" rule
isolates correctly and a shared target leaks. Process-per-tenant matches that model exactly, works for
stdio (which has no headers to inject into), and trusts nothing the caller sends. Per-request claims can
come with a native Streamable HTTP transport (audit P2 item 5), as an addition, not a replacement.

### Conformance over MCP runs on an isolated scratch store of the same backend kind, or refuses

`create_mcp_server(..., scratch_factory=…)`. The CLI builds one for SQLite (a temp file, same embedder);
for Postgres it builds none — a scratch schema needs DDL the runtime role does not have — and the tools
return a clear refusal pointing at `grafomem conformance -b postgres --db <scratch>`. Refusing is preferred
to polluting. A test counts the serving store's rows directly (not through the tenant-scoped list, which
would hide synthetic rows written under other tenants — that first version of the test was vacuous and
the flip caught it).

### Verdicts come from the engine; nothing is defaulted

`PASS`/`VIOLATION` straight from `CapabilityResult.passed`, with the `DirectionResult`s (name, objective,
point, CI, passed) returned as the metrics. Not declared → `NOT_DECLARED`, `claim_matches_behavior: null`.
Declared but no result → `UNTESTED`. A verify failure → `ERROR: <reason>` on every flag, never `UNTESTED`.
`evidence.rollup_hash` is a BLAKE2b over the actual results; `corpus_hash` is the corpus lock's hash when
the lock file is present and `null` otherwise — never a hash of the seed. `evidence.store` names the
scratch instance that was tested. The three tests that mocked the engine's shape are replaced by tests
through the engine's own reference backends (`SoftDeleteBackend` → VIOLATION, `HonestDeleteBackend` →
PASS, `LeakyTenant` → VIOLATION, `TenantScoped` → PASS).

### Stdout is the protocol

In MCP mode every human-readable line — banner, tenant, scratch store, erasure-service status — goes to
stderr, in both transports.

## Consequences

- `grafomem serve --mcp` now **requires `--tenant`** with the shipped backends. The audit's repro script
  needs `GRAFOMEM_MCP_TENANT` set. This is the intended break.
- `run_conformance` over MCP on Postgres returns an error until a scratch-schema strategy with the right
  role exists; the CLI path is unaffected.
- The `run_conformance` response shape changes: `directions`, `workload`, `evidence.store` added;
  `bootstrap_ci`, `ops_until_gone`, `isolation_conformance` (never real) removed.
- Real-backend MCP tests (`tests/test_mcp_real_backends.py`: SQLite and Postgres, the audit's two-tenant
  scenario verbatim, store-level pollution count, stdout silence, CLI tenant gate) now stand beside the
  mock-based ones. Every guard proven by flipping it.
- Out of scope, still open from the audit: native Streamable HTTP (5), `import grafomem` needing numpy (6),
  tiktoken egress at Postgres startup (7), repository placement of Cloud code (8, ADR-0005 — operator
  decision). The fixes in 0016/0017/0018 are unreleased until a version bump: PyPI `0.4.2` still fails
  exactly as the audit describes.
