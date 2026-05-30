# R5 — Governed World-Model (drop-in, same pattern as R1/R3)

A typed graph that is **governed, not just stored**: a signed type registry (Object / Link /
Action), object & link validation, and gated action invocation with a trust-tier authority
check plus a signed, attributable invocation receipt. This is the capability that separates a
governed ontology from an ungoverned one — every action type carries a required trust tier,
runs through your `GovernanceGateway`, and produces a cryptographically verifiable record of
who did what to which objects under which decision.

Built with all hardened patterns: `document`-column verify, string timestamp, `Jsonb` binding,
deny/HITL gateway handling.

## Where each file goes

| File | Destination |
|---|---|
| `world_model.py` | `src/aml/cloud/world_model.py` |
| `world_model_routes.py` | `src/aml/cloud/world_model_routes.py` |
| `sdk_world_model.py` | `sdk/src/grafomem/services/world_model.py` |
| `world_model_self_conformance.py` | `tests/world_model_self_conformance.py` |

## Wiring — `src/aml/server/app.py`, after the artifact-registry block

```python
from aml.cloud.world_model import WorldModelService
from aml.cloud.world_model_routes import create_world_model_router
wm = WorldModelService(db_url, signing_key=erasure_key, gateway=gg, decision_trail=dt)
wm.ensure_schema()
app.state.world_model = wm
app.include_router(create_world_model_router(wm))
```

## SDK — `sdk/src/grafomem/client.py`

```python
from grafomem.services.world_model import WorldModelService as _WM
self._world_model = _WM(self._http)
@property
def world_model(self) -> _WM:
    return self._world_model
```

## Endpoints (under `/v1/world-model`)

Types: `POST /types` · `GET /types` · `GET /types/{id}` · `GET /types/{id}/verify`.
Validation: `POST /validate/object` · `POST /validate/link`.
Governed actions: `POST /actions/invoke` · `GET /actions/{id}` · `GET /actions/{id}/verify` ·
`POST /actions/{id}/approve|reject` · `GET /actions/stats`.

## The crypto-governed Action Type

`invoke_action` enforces, in order:

1. **Authority** — the caller's `trust_tier` must meet the action's `required_trust_tier`
   (TierGate ordering untrusted < basic < verified < trusted < release < root). Below → refused.
2. **Params** — validated against the action's `input_schema`.
3. **Governance** — `evaluate_and_gate(tenant, action.operation, ctx)`; deny → refused,
   escalate → parked for HITL, resumable via `approve`/`reject`.
4. **Receipt** — on allow, an Ed25519-signed invocation receipt recording who (GEIANT
   delegation + tier) did what action to which subjects under which decision — verifiable and
   tamper-evident, exactly like your certificates.

That ordered chain is the difference between "the ontology has an `approve_payment` action" and
"only a release-tier principal, cleared by policy, can invoke it, and every invocation is
attributable." gcrumbs note: an invocation is step-shaped (unlike the certificates), so this is
the natural place to later chain via `execution_receipts.issue_receipt(...)` — left as a hook.

## Run the suite

```bash
GRAFOMEM_DB_URL="postgresql://grafomem:grafomem_dev@localhost:5432/grafomem" \
  python3 tests/world_model_self_conformance.py
```

Ten gates, non-vacuous: W2 rejects a link to an unknown type, W5 tampers a type receipt in
Postgres, W6/W7 reject bad instances/links, W9 proves the gateway is enforced, and **W10 proves
an under-authorized caller is refused** — the governed-action crux.

## ADAPT (same small list)

1. `erasure_key` — reuse it (or a dedicated `worldmodel_key`).
2. `_get_conn()` — per-call; swap to your pool if preferred.
3. SDK transport — align `self._http.*` to `services/erasure.py`.
