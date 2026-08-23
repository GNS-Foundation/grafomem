# grafomem-cgr capture MCP server (dogfood)

Track C, ticket 1. Lets GRAFOMEM's own Claude agents (Claude Code / Cowork sessions) accumulate CGR substrate from their dev-loop judgments — **capture now, score later.** Wraps the existing governed HTTP path; no new endpoints, no scoring change, no new crypto.

## Config (env)

| var | meaning |
|---|---|
| `GRAFOMEM_API` | base URL (default `https://api.grafomem.com`) |
| `GRAFOMEM_CGR_TENANT_KEY` | the **dogfood** tenant's `X-API-Key` (the sensitive secret) |
| `GRAFOMEM_CGR_DOGFOOD_TENANT` | the expected dogfood tenant_id (never-corp guard, verified at runtime against the API key's real tenant) |
| `GRAFOMEM_CGR_ROLE_KEYS` | path to JSON mapping role handles → `agent_key` (public Ed25519 hex) |

Example `role_keys.json`:
```json
{ "cc-builder@ulissy": "<64-hex pubkey>", "cowork-architect@ulissy": "<64-hex pubkey>" }
```

Key custody: this config holds **only** the dogfood role public keys + the dogfood tenant credential. The caller picks a role *handle*; the server injects that role's key — a caller can't supply an arbitrary key or another tenant. Guards: refuses corp at config load **and** if the API key's resolved tenant is corp/mismatched (checked on each decision response).

Billing: governed decisions **meter as real usage** on the dogfood tenant's plan (~a handful/day — trivially inside any allotment). `serve` and `selftest` print `GET /v1/usage/current` so it's never a surprise.

## Commands

```bash
# 1) (optional) register the role identities on the dogfood tenant — idempotent, never corp
python ops/cgr_capture_mcp.py setup

# 2) full-loop acceptance: decision → outcome → score movement (the "first real CGR evidence" moment)
python ops/cgr_capture_mcp.py selftest --handle cc-builder@ulissy --domain deploy-verification

# 3) run the MCP server over stdio (connect from Claude Code / Cowork)
python ops/cgr_capture_mcp.py serve
```

## Connect to Claude Code

Add to your MCP config (e.g. `.mcp.json` / client settings), with the env vars set:
```json
{
  "mcpServers": {
    "grafomem-cgr": {
      "command": "python",
      "args": ["ops/cgr_capture_mcp.py", "serve"],
      "env": {
        "GRAFOMEM_CGR_TENANT_KEY": "…",
        "GRAFOMEM_CGR_DOGFOOD_TENANT": "…",
        "GRAFOMEM_CGR_ROLE_KEYS": "/abs/path/role_keys.json"
      }
    }
  }
}
```

Then the session can call `cgr_record_decision` at judgment time and `cgr_record_outcome` when the result resolves.

## Tools

- **`cgr_record_decision`** — `{work_item_id, agent_handle, domain, decision, verifiability_tag?, reason_code?, reason_text?, agent_confidence?}`. `domain` ∈ `deploy-verification | security-scan | adversarial-review`; `decision` ∈ `certify | reject`. Only `judgment`+`certify` moves the score.
- **`cgr_record_outcome`** — `{work_item_id, result, source?}`. `result` is mapped to success/failure; an unmapped result is a no-op (decision left **pending**, never falsely resolved).

## v0 scope (flagged)

CGR **scoring** is single-dimension today, so per-domain *scoring* is the Phase-2 "generalize substrate schema" follow-up (it touches the served scoring surface). The role's score currently appears under the existing dimension.

**Domain durability (this is the load-bearing part):** the `domain` string is stored **durably server-side, per decision, in the never-encrypted CGR-readable decision `parameters` as `cgr_domain`** — surfaced by the substrate loader as `DecisionRow.cgr_domain` and in `/v1/cgr/substrate/export`. It lives in the **signed decision record**, not in any client-side log or config mapping. So per-domain re-scoring later is real, not a hope. (This needed a small, additive, backward-compatible change to the governed-decision write; the historical export shape is unchanged — `cgr_domain` is appended.)
