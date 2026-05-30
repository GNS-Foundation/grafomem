# Phase-B drop-in — `landing_service` (WIRED to your real signatures)

These are rewired to match what your repo actually exposes (from the grep): function-based
`sign_provenance`, `GovernanceGateway.evaluate_and_gate(...) -> (allowed, logs)`, the
`erasure_proof.py` service shape (`db_url` + `signing_key`, `_get_conn`, `ensure_schema`),
the `create_*_router(service)` factory, and `request.state.tenant.tenant_id`.

**Design change from the first cut:** your gcrumbs (`execution_receipts`) is chain-only and
step-oriented, and your **erasure certs are sign-only (not chained)**. So v1 landing is
**sign-only too — a near-clone of `erasure_proof.py`**. The Merkle-epoch *anchor* (inclusion
proofs) is deferred behind `epoch_anchor=False`; flip it on when `roll_epoch`/`get_proof`
land (the CDP Sprint-1 extension). No separate SQL migration — `ensure_schema()` creates the
table, exactly like the erasure service.

## Where each file goes

| File | Destination |
|---|---|
| `landing_service.py` | `src/aml/cloud/landing_service.py` |
| `landing_routes.py` | `src/aml/cloud/landing_routes.py` |
| `sdk_landing.py` | `sdk/src/grafomem/services/landing.py` |
| `landing_certificates.sql` | reference only — `ensure_schema()` already creates it |

## Wiring (matches your `app.py` lifespan)

**`src/aml/server/app.py` — right after `gg = GovernanceGateway(db_url)`:**
```python
from aml.cloud.landing_service import LandingService
from aml.cloud.landing_routes import create_landing_router
# landing_key: derive the same way you derive erasure_key from config.signing_key
ls = LandingService(db_url, signing_key=landing_key, gateway=gg, decision_trail=dt,
                    epoch_anchor=False)
ls.ensure_schema()
app.state.landing_service = ls
app.include_router(create_landing_router(ls))
```

**`sdk/src/grafomem/client.py` — mirror the erasure property:**
```python
from grafomem.services.landing import LandingService as _Landing
# in __init__, next to self._erasure = ErasureService(self._http):
self._landing = _Landing(self._http)
# with the other @property blocks:
@property
def landing(self) -> _Landing:
    return self._landing
```

## ADAPT — what's left to confirm (small now)

1. **`landing_key`** — how does `app.py` derive `erasure_key` from `config.signing_key`? Use the identical derivation for `landing_key` (or reuse the same key — same Ed25519 family).
2. **`_get_conn()`** — the skeleton opens `psycopg.connect(db_url, row_factory=dict_row, autocommit=True)` per call. If your `erasure_proof._get_conn()` instead reuses a single connection or checks out from `app.state.db_pool`, copy that pattern (one line).
3. **SDK transport** — open `sdk/src/grafomem/services/erasure.py` and match its exact `self._http.<method>(...)` calls (the skeleton assumes `.post(path, json=...)` / `.get(path, params=...)`).

Everything else — signing (`sign_provenance(seed, digest) -> (sig, pub)`), the gateway
`(allowed, logs)` contract with `EvaluationResult.DENIED`/`.ESCALATED`, the tenant lookup,
the BLAKE2b-128 cert id — is already wired to your real signatures.

## Enforcing HITL on issuance

`evaluate_and_gate(tenant, "landing.issue", ctx)` returns `allowed=True` if **no policy
triggers**. To require human approval on certificate issuance, add a governance policy keyed
on `operation == "landing.issue"` with action `ESCALATE` (your `PolicyAction.ESCALATE`). The
service already handles the escalation: it parks the cert as `waiting_hitl` and returns `202`,
resumable via `POST /v1/landing/{id}/approve|reject`.

## Conformance (graduate the gates → 49/49)

Port `landing/conformance/run_phase1.py` + `gates.py` into
`tests/landing_self_conformance.py` (next to `tests/gmp_self_conformance.py`), pointing the
harness at the live service: `issue → get → verify → tamper-a-field → verify fails`. That
folds the ten landing gates into your suite.

## After this service

`artifact_registry.py` (R1) and `world_model.py` (R5) follow the identical shape: a
`db_url`+`ensure_schema` service, a `create_*_router` factory, lifespan construction after the
gateway, an SDK service + client property.
