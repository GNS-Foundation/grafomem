# R4 — Composition Governance (the final capability)

Governs how certified artifacts may **combine**. A composition proposes members (each a certified
artifact reference + its license), a kind, and a target ref. The service enforces composition
policy — every member certified, licenses mutually compatible, composer tier cleared — runs the
GovernanceGateway, and issues a signed composition receipt. The composed result can be registered
back in R1, closing the cycle:

    R2 corpus -> R1 artifact -> R3 certificate -> R4 composition -> R1 register the composed result

## Where each file goes

| File | Destination |
|---|---|
| `composition_governance.py` | `src/aml/cloud/composition_governance.py` |
| `composition_governance_routes.py` | `src/aml/cloud/composition_governance_routes.py` |
| `sdk_compositions.py` | `sdk/src/grafomem/services/compositions.py` |
| `composition_governance_self_conformance.py` | `tests/composition_governance_self_conformance.py` |

## Wiring — `src/aml/server/app.py`, after the provenance-customs block

```python
from aml.cloud.composition_governance import CompositionGovernanceService
from aml.cloud.composition_governance_routes import create_composition_governance_router
cg = CompositionGovernanceService(db_url, signing_key=erasure_key, gateway=gg, decision_trail=dt)
cg.ensure_schema()
app.state.composition_governance = cg
app.include_router(create_composition_governance_router(cg))
```

## SDK — `sdk/src/grafomem/client.py`

```python
from grafomem.services.compositions import CompositionsService
self._compositions = CompositionsService(self._http)
@property
def compositions(self) -> CompositionsService:
    return self._compositions
```

## Endpoints (under `/v1/compositions`)

`POST /` (govern a composition) · `GET /` · `GET /{id}` · `GET /{id}/verify` ·
`GET /{id}/artifact` (descriptor to register the composed result in R1) · `GET /stats` ·
`POST /{id}/approve|reject` (HITL).

## Composition policy

`compose` enforces, before the gateway:

1. **Certified members** — every member must carry `certified: true`; an uncertified member is refused.
2. **License compatibility** — no no-derivatives license, and no non-commercial mixed with
   commercial (pluggable ruleset in `licenses_compatible`).
3. **Composer authority** — the composer's `trust_tier` must meet `required_trust_tier`.

Then `evaluate_and_gate(tenant, "composition.govern", ctx)` (deny → refused, escalate → HITL),
and on approval a signed receipt attesting the members, kind, license verdict, and authority.
Identity is content-addressed and order-independent (same members + kind = same composition).

## Run the suite

```bash
GRAFOMEM_DB_URL="postgresql://grafomem:grafomem_dev@localhost:5432/grafomem" \
  python3 tests/composition_governance_self_conformance.py
```

Ten gates, non-vacuous: K4 proves layered tamper-evidence (member tamper caught by the
members-digest, signed-field tamper by the signature), K6 refuses an uncertified member, K7
refuses incompatible licenses, K8 refuses an under-authorized composer.

## ADAPT (same small list)

1. `erasure_key` — reuse it (or a dedicated `composition_key`).
2. `_get_conn()` — per-call; swap to your pool if preferred.
3. SDK transport — align `self._http.*` to `services/erasure.py`.
