# grafomem-cgr capture MCP server (dogfood)

> **Where the code lives (changed).** The implementation moved to the publishable package
> [`packages/grafomem-cgr/`](../packages/grafomem-cgr/), which is what ships to PyPI and the
> MCP Registry as `com.grafomem/cgr-capture`. `ops/cgr_capture_mcp.py` is now GRAFOMEM's
> **internal wrapper**: it adds our own policy — the corp tenant on the forbidden denylist —
> and is what the dogfood launcher points at. **The dogfood loop is unchanged**: the launcher,
> its env vars, and every guard behave exactly as before.
>
> The never-corp guard generalized: instead of a hardcoded corp constant, the package takes a
> required tenant **pin** (`GRAFOMEM_CGR_TENANT`) plus an optional **denylist**
> (`GRAFOMEM_CGR_FORBIDDEN_TENANTS`). `ops/` always injects corp into that denylist, so corp is
> refused here exactly as it was. `GRAFOMEM_CGR_DOGFOOD_TENANT` is still honoured as the
> pre-1.0 name of the pin.

Track C, ticket 1. Lets GRAFOMEM's own Claude agents (Claude Code / Cowork sessions) accumulate CGR substrate from their dev-loop judgments — **capture now, score later.** Wraps the existing governed HTTP path; no new endpoints, no scoring change, no new crypto.

## Config (env)

| var | meaning |
|---|---|
| `GRAFOMEM_API` | base URL (default `https://api.grafomem.com`) |
| `GRAFOMEM_CGR_TENANT_KEY` | the **dogfood** tenant's `X-API-Key` (the sensitive secret) |
| `GRAFOMEM_CGR_TENANT` | the expected tenant_id (tenant pin, verified at runtime against the API key's real tenant). `GRAFOMEM_CGR_DOGFOOD_TENANT` is the pre-1.0 name and still works — the dogfood launcher uses it. |
| `GRAFOMEM_CGR_FORBIDDEN_TENANTS` | comma-separated tenant_ids always refused. `ops/` injects corp automatically. |
| `GRAFOMEM_CGR_ROLE_KEYS_JSON` | the role-keys mapping inline as JSON, instead of a file path (used by the public env-only config) |
| `GRAFOMEM_CGR_ROLE_KEYS` | path to JSON mapping role handles → `agent_key` (public Ed25519 hex) |

Example `role_keys.json`:
```json
{ "cc-builder@ulissy": "<64-hex pubkey>", "cowork-architect@ulissy": "<64-hex pubkey>" }
```

### Key files — which file the tool reads (reconciled)

Two distinct files, one read by the tool and one not — **do not confuse them**:

| file | contents | read by the tool? |
|---|---|---|
| **`role_keys.json`** (e.g. `~/.grafomem-dogfood-role_keys.json`) | role handle → **public** `agent_key` (the CGR binding subject) | **YES** — this is what `GRAFOMEM_CGR_ROLE_KEYS` points at. **This one wins.** |
| `role_privkeys.json` (e.g. `~/.grafomem-dogfood-role_privkeys.json`) | role handle → **private** key | **NO** — harness custody only; kept for future per-decision proof-of-possession / key rotation. The v0 capture path uses only the public key. |

`GRAFOMEM_CGR_ROLE_KEYS` **must point at the public `role_keys.json`.** If it were pointed at `role_privkeys.json`, decisions would be attributed to the wrong `agent_key` (the private-key hex, not the subject pubkey) and the score would bind to a non-identity. The public file is canonical.

Key custody: this config holds **only** the dogfood role public keys + the dogfood tenant credential. The caller picks a role *handle*; the server injects that role's key — a caller can't supply an arbitrary key or another tenant. Guards: refuses corp at config load **and** if the API key's resolved tenant is corp/mismatched (checked on each decision response).

Billing: governed decisions **meter as real usage** on the dogfood tenant's plan (~a handful/day — trivially inside any allotment). `serve` and `selftest` print `GET /v1/usage/current` so it's never a surprise.

## Sequencing (matters — the domain-durability change is server-side)

`domain` durability lives in the deployed API, not just this client. Run in this order:

1. launch window closes → **merge** the branch → **deploy** (Railway) so the API has the `domain` field.
2. **then** `setup` + `selftest` with the dogfood creds.

Do **not** run `selftest`/`serve` against an API that predates the change: Pydantic would silently ignore the unknown `domain` field and decisions would land **without** `cgr_domain`. `selftest` has a **durability guard** that catches exactly this — after recording the decision it re-reads it from `/v1/cgr/substrate/export` and **aborts loudly** if `cgr_domain` didn't round-trip (before recording the outcome / claiming a score).

## Commands

```bash
# 1) (after deploy) register the role identities on the dogfood tenant — idempotent, never corp
python ops/cgr_capture_mcp.py setup

# 2) full-loop acceptance (with durability guard): decision → verify cgr_domain persisted → outcome → score movement
python ops/cgr_capture_mcp.py selftest --handle cc-builder@ulissy --domain deploy-verification

# 3) run the MCP server over stdio (connect from Claude Code / Cowork)
python ops/cgr_capture_mcp.py serve
```

## Connect to Claude Code / Cowork

**Recommended: a launcher script so no secret lives in the MCP config.** Point the MCP config at a small launcher that reads the chmod-600 creds file and starts the server; the API key never appears in any Claude Code config.

Launcher (`~/.grafomem-dogfood-launch.sh`, `chmod 700`):
```bash
#!/usr/bin/env bash
set -euo pipefail
REPO="$HOME/grafomem"; CREDS="${GRAFOMEM_CGR_CREDS:-$HOME/.grafomem-dogfood-creds.json}"; PY="$REPO/.venv/bin/python"
export GRAFOMEM_API="${GRAFOMEM_API:-https://api.grafomem.com}"
export GRAFOMEM_CGR_TENANT_KEY="$("$PY" -c "import json;print(json.load(open('$CREDS'))['api_key'])")"
export GRAFOMEM_CGR_DOGFOOD_TENANT="$("$PY" -c "import json;print(json.load(open('$CREDS'))['tenant_id'])")"
export GRAFOMEM_CGR_ROLE_KEYS="${GRAFOMEM_CGR_ROLE_KEYS:-$HOME/.grafomem-dogfood-role_keys.json}"   # PUBLIC keys
exec "$PY" "$REPO/ops/cgr_capture_mcp.py" serve
```

Register it (user scope → all local Claude Code + Cowork sessions):
```bash
claude mcp add grafomem-cgr --scope user -- $HOME/.grafomem-dogfood-launch.sh
```
Or add to the MCP config (`~/.claude.json` user scope, or a project `.mcp.json`):
```json
{ "mcpServers": { "grafomem-cgr": { "command": "/abs/path/.grafomem-dogfood-launch.sh" } } }
```

If a bad/missing creds path breaks the launcher, it exits non-zero and the server simply doesn't load — the session continues **unblocked** (fail-open); no tools appear until the path is restored.

Then the session can call `cgr_record_decision` at judgment time and `cgr_record_outcome` when the result resolves.

## Tools

- **`cgr_record_decision`** — `{work_item_id, agent_handle, domain, decision, verifiability_tag?, reason_code?, reason_text?, agent_confidence?}`. `domain` ∈ `deploy-verification | security-scan | adversarial-review`; `decision` ∈ `certify | reject`. Only `judgment`+`certify` moves the score.
- **`cgr_record_outcome`** — `{work_item_id, result, source?}`. `result` is mapped to success/failure; an unmapped result is a no-op (decision left **pending**, never falsely resolved).

## v0 scope (flagged)

CGR **scoring** is single-dimension today, so per-domain *scoring* is the Phase-2 "generalize substrate schema" follow-up (it touches the served scoring surface). The role's score currently appears under the existing dimension.

**Domain durability (this is the load-bearing part):** the `domain` string is stored **durably server-side, per decision, in the never-encrypted CGR-readable decision `parameters` as `cgr_domain`** — surfaced by the substrate loader as `DecisionRow.cgr_domain` and in `/v1/cgr/substrate/export`. It lives in the **signed decision record**, not in any client-side log or config mapping. So per-domain re-scoring later is real, not a hope. (This needed a small, additive, backward-compatible change to the governed-decision write; the historical export shape is unchanged — `cgr_domain` is appended.)
