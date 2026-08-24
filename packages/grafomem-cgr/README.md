# grafomem-cgr

<!-- mcp-name: com.grafomem/cgr-capture -->

An MCP server that lets a coding agent **record its own judgments and how they turned out**,
against your [GRAFOMEM Cloud](https://grafomem.com) tenant.

The model is **capture now, score later.** Every time your agent makes a call it could be
wrong about — "this deploy is safe", "this scan is clean", "this review finding is real" —
it records the judgment. Later, when reality settles, it records the outcome. The two are
joined by a `work_item_id` you choose. What accumulates is an append-only, attributable
record of judgments and their results.

This package is the **capture** half only. It writes to your tenant; it does not compute,
serve, or display scores.

## What it is not

- It does not score anything at capture time, and it does not read scores back into your session.
- It does not make your agent safer or gate anything. Nothing is blocked, approved, or prevented.
- It is not a compliance, audit, or security product.
- Scoring on GRAFOMEM Cloud is **single-dimension today**: all judgment/certify decisions
  score under one dimension regardless of `domain`. The `domain` you pass is stored durably
  per decision (see below), so it can be attributed later — but it is not scored separately yet.

## Requirements

A GRAFOMEM Cloud account. The **free tier is sufficient** — expected volume is a handful of
decisions per day. Note that governed decisions **meter as real usage** on your plan; the
server prints your current usage on startup so this is never a surprise.

## Install & configure

One command, plus environment variables — no repo checkout, no launcher script.

Claude Code:

```bash
claude mcp add grafomem-cgr --scope user \
  --env GRAFOMEM_CGR_TENANT_KEY=sk_your_api_key \
  --env GRAFOMEM_CGR_TENANT=your_tenant_id \
  --env GRAFOMEM_CGR_ROLE_KEYS_JSON='{"cc-builder@acme":"<64-hex public key>"}' \
  -- uvx grafomem-cgr
```

Or any MCP client that reads a JSON config:

```json
{
  "mcpServers": {
    "grafomem-cgr": {
      "command": "uvx",
      "args": ["grafomem-cgr"],
      "env": {
        "GRAFOMEM_CGR_TENANT_KEY": "sk_your_api_key",
        "GRAFOMEM_CGR_TENANT": "your_tenant_id",
        "GRAFOMEM_CGR_ROLE_KEYS_JSON": "{\"cc-builder@acme\":\"<64-hex public key>\"}"
      }
    }
  }
}
```

### Environment

| Variable | Required | Meaning |
|---|---|---|
| `GRAFOMEM_CGR_TENANT_KEY` | **yes** | Your tenant's `X-API-Key`. Needs `decisions:read` for the durability guard. |
| `GRAFOMEM_CGR_TENANT` | **yes** | The tenant id you expect that key to resolve to. If the key resolves anywhere else, every write is refused. |
| `GRAFOMEM_CGR_ROLE_KEYS_JSON` | one of | Role handles → public `agent_key`, inline as JSON. |
| `GRAFOMEM_CGR_ROLE_KEYS` | these two | Same mapping, as a path to a JSON file. |
| `GRAFOMEM_CGR_FORBIDDEN_TENANTS` | no | Comma-separated tenant ids to **always** refuse, even if pinned by mistake. Put your production tenant here. |
| `GRAFOMEM_API` | no | Base URL. Defaults to `https://api.grafomem.com`. |

### Role keys

A role handle (`cc-builder@acme`) is the identity a judgment is attributed to. Its
`agent_key` is a stable **Ed25519 public key, 64 hex characters** — the subject the record
binds to. Generate one per role:

```bash
openssl genpkey -algorithm Ed25519 -out cc-builder.pem
openssl pkey -in cc-builder.pem -pubout -outform DER | tail -c 32 | xxd -p -c 64
```

Keep the private half. This version does not use it — it binds to the public key only —
but per-decision proof-of-possession is planned hardening, and holding the private key is
what will let you prove the identity is yours.

## Tools

**`cgr_record_decision`** — record a judgment.

| arg | |
|---|---|
| `work_item_id` | stable join key you choose (PR number, task id, run id) |
| `agent_handle` | one of your configured role handles |
| `domain` | `deploy-verification` \| `security-scan` \| `adversarial-review` |
| `decision` | `certify` \| `reject` |
| `verifiability_tag` | `judgment` (default) \| `rule` |
| `reason_code`, `reason_text` | optional |
| `agent_confidence` | optional, **accepted but not yet persisted** — see below |

`agent_confidence` is accepted by the tool schema for forward compatibility but is not
currently written to the decision record. Don't rely on it being stored.

Only `judgment` + `certify` moves a score. A `rule` decision is deterministic and carries
no reputational weight — tag honestly.

**`cgr_record_outcome`** — record how it turned out.

| arg | |
|---|---|
| `work_item_id` | the same join key used at decision time |
| `result` | a result label, e.g. `deploy_succeeded`, `ci_failed`, `vuln_found`, `review_confirmed` |
| `source` | optional provenance string |

**An unrecognised `result` is a deliberate no-op.** The decision stays pending rather than
being resolved on a guess — falsely resolving a decision corrupts the record permanently.
Mapped labels are: `deploy_succeeded`, `deploy_healthy`, `ci_passed`, `migration_applied`,
`merge_landed`, `pr_merged`, `scan_clean`, `no_vuln_confirmed`, `review_confirmed`,
`finding_correct`, `bug_confirmed` (success); `deploy_failed`, `deploy_rolled_back`,
`migration_failed`, `ci_failed`, `merge_reverted`, `vuln_found`, `secret_found`,
`review_refuted`, `finding_wrong`, `bug_not_real` (failure).

## Safety properties

- **Tenant pinning.** You declare the tenant up front. The pin is enforced at startup *and*
  re-checked against the tenant the API actually resolved on every decision response — so a
  rotated or mistaken key cannot quietly write somewhere else.
- **Denylist.** `GRAFOMEM_CGR_FORBIDDEN_TENANTS` refuses named tenants outright.
- **Key custody.** The caller picks a role *handle*; the server injects that role's key. A
  tool call can never supply an arbitrary key or target another tenant.
- **Fail-open.** If the server is misconfigured it exits non-zero and simply doesn't load.
  Your session continues unblocked, with no tools registered. A refused tool call returns
  an error as normal tool output rather than crashing the MCP server.
- **Domain durability.** `domain` is sent as a dedicated field and stored server-side, per
  decision, in the never-encrypted CGR-readable decision `parameters` as `cgr_domain` — in
  the signed decision record, not in a client-side log. `selftest` includes a **durability
  guard** that re-reads the decision it just wrote and aborts loudly if `cgr_domain` did not
  round-trip, rather than silently recording a domainless decision.

## CLI

```bash
grafomem-cgr                 # run the stdio MCP server (the default)
grafomem-cgr setup           # register your role identities on the tenant (idempotent)
grafomem-cgr selftest --handle cc-builder@acme --domain deploy-verification
```

`selftest` closes the loop once — decision → durability guard → outcome → score read — and
prints the movement and the meter. Run it before wiring the server into a session.

## Links

- [grafomem.com](https://grafomem.com) · [docs.grafomem.com](https://docs.grafomem.com)
- [Source](https://github.com/GNS-Foundation/grafomem) · [Issues](https://github.com/GNS-Foundation/grafomem/issues)

MIT licensed.
