# Phase-B drop-in — `landing_service` skeletons

Four review-first skeletons. They land in your **core** `src/aml/` tree, so place each
deliberately (don't blind-unzip at root). Every external call is marked `# ADAPT:` — wire
it to your real signatures, then delete the marker.

## Where each file goes

| Skeleton file | Destination |
|---|---|
| `landing_service.py` | `src/aml/cloud/landing_service.py` |
| `landing_routes.py` | `src/aml/cloud/landing_routes.py` |
| `landing_certificates.sql` | apply to your DB (table #23) |
| `sdk_landing.py` | `sdk/src/grafomem/services/landing.py` |

## Wiring

**1. `src/aml/server/app.py` — lifespan (init after gcrumbs + gateway, slot 9):**
```python
from aml.cloud.landing_service import LandingService
# ...inside lifespan(), after ExecutionReceiptService (gcrumbs) and GovernanceGateway:
app.state.landing_service = LandingService(
    db=db_pool, signer=signer, gcrumbs=execution_receipts, gateway=governance_gateway,
    tenants=tenant_manager, compliance=compliance_tracker, metering=metering_service,
    artifacts=getattr(app.state, "artifact_registry", None),
    epoch_layer=False,   # <-- set True once roll_epoch/get_proof exist (see §6 of the scope)
)
```

**2. `src/aml/server/app.py` — register the router:**
```python
from aml.cloud.landing_routes import router as landing_router
app.include_router(landing_router)
```

**3. `sdk/src/grafomem/client.py` — lazy property (mirror the erasure one):**
```python
from .services.landing import LandingService as _Landing
# ...inside GrafomemClient:
@property
def landing(self):
    if self._landing is None:
        self._landing = _Landing(self._http)
    return self._landing
```

## The `epoch_layer` switch (the one real decision)

- `epoch_layer=False` (default): the cert is anchored as a **chained receipt** via
  `gcrumbs.issue_receipt(...)`, verified by your existing `verify-chain`. Ships today.
- `epoch_layer=True`: the anchor uses `roll_epoch()` + `get_proof((epoch_id, leaf_index))`
  for the O(log N) **inclusion proof** — the same gcrumbs epoch API your CDP Sprint-1 plan
  adds (`GET /v1/decisions/{id}/proof`). Flip the flag when that lands; no other change.

## ADAPT checklist (confirm against your real code)

1. **Signer** — `src/aml/provenance.py`: replace the local `canon/b2_256/b2_128` if you prefer the platform versions; wire `self.signer.sign(bytes)->hex` and `self.signer.public_key_hex` to your Ed25519 signer (same key family as decision/erasure certs).
2. **Gateway** — `GovernanceGateway.evaluate_and_gate(tenant, action, context)`: confirm the return type and the status values (`ALLOWED`/`DENIED`/`ESCALATED` vs your enum). The skeleton handles both object- and dict-shaped returns.
3. **gcrumbs** — `ExecutionReceiptService`: confirm `issue_receipt(...)` params (the skeleton passes `tenant_id, kind, payload` — adjust to your real signature, e.g. the step-oriented one), and, for `epoch_layer=True`, `roll_epoch(agent_id=...)` + `get_proof((epoch_id, leaf_index))`.
4. **db** — `db_pool`: confirm the `connection()` / cursor / `commit()` pattern (the skeleton uses `with self.db.connection() as conn, conn.cursor() as cur:`).
5. **auth/tenant** — `landing_routes._tenant`: read the tenant your auth middleware already resolved from `X-API-Key`.
6. **service access** — `landing_routes._svc`: reach the instance the way your app does (the skeleton assumes `app.state.landing_service`).
7. **verification internals** — `_verify_sig` / `_verify_delegation` / `_verify_anchor`: wire to `provenance.ed25519_verify` and the gcrumbs proof check (they return `True` as placeholders).

## Conformance (graduate the gates)

Port `landing/conformance/run_phase1.py` + `gates.py` into `tests/landing_self_conformance.py`
next to `tests/gmp_self_conformance.py`, pointing the harness at the live service (issue →
verify → tamper) instead of the in-memory reference. That makes the ten gates part of your
39/39 suite (→ 49/49).

## After this service

`artifact_registry.py` (R1) and `world_model.py` (R5) follow the same pattern: a JSONB-backed
table, a `/v1/...` router, lifespan wiring, an SDK service, and the gcrumbs/gateway reuse.
