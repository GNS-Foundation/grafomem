# HITL attest/fetch/inbox — RLS 404 regression + fix (Option A)

## Finding

The Track-2 graduation gate (a live HITL cycle on a dedicated test tenant) surfaced a **blocking prod
bug**: `POST /v1/hitl/requests/{id}/attest`, `GET /v1/hitl/requests/{id}` (fetch), and
`GET /v1/hitl/approvers/{id}/requests` (inbox) return **404 / empty** for requests that exist,
under `grafomem_rt` + RLS.

**Root cause.** These three endpoints authenticate the approver by **Ed25519 signature, not a tenant
API key** — they are self-authenticated. The auth middleware (`auth.py:238-250`) therefore skips
API-key auth for `/v1/hitl/requests/{id}*` (except `/verify`) and `/v1/hitl/approvers/*` and pins
their tenant context to `DEFAULT_NAMESPACE = "default_namespace"`. Their lookups key on
`request_id` / `approver_id` **only** and rely on RLS for tenant scoping. Under FORCE ROW LEVEL
SECURITY on `hitl_approval_requests` + `hitl_approvers`, the `default_namespace` context matches no
real tenant → 0 rows → 404 / empty inbox. Pre-flip (no RLS) the `WHERE request_id` lookup found the
row regardless. `list_requests` and `verify` are unaffected — they require an API key and filter by
`request.state.tenant.tenant_id` explicitly.

**Impact.** Broke at the Track-1 RLS flip. Every prod HITL approval has 404'd since — the Phase-2
governed **send** loop's approve path *and* the governed dev loop. (Confirm whether any live attest
has succeeded in prod since the flip — expected: none.)

**Why tests missed it.** `test_hitl_attest_execute` / `test_hitl_routes` / `test_hitl_unauthorized`
use **mock** db pools (no RLS); `test_rls_decision_hitl` e2e drives **token-authenticated** endpoints,
never the self-authenticated auth-skip path. No test ran the real attest/fetch/inbox path under FORCE
RLS as a non-bypass role.

## Decision — Option A (resolve tenant, don't bypass or API-key it)

Rejected: **B** (caller-supplied tenant = caller controls the RLS GUC) and **C** (require a tenant API
key = breaks the pure-signer approver model). Chosen: resolve the owning tenant server-side via a
`SECURITY DEFINER` function that returns **only** the tenant id, then scope the connection and run the
existing logic under correct RLS.

## Implementation

- `ops/hitl_tenant_resolvers.sql` — two `SECURITY DEFINER` functions:
  `hitl_request_tenant(request_id) → text` and `hitl_approver_tenants(approver_id) → text[]`. They
  return **only** tenant id(s), `SET search_path`, `REVOKE EXECUTE FROM PUBLIC`, `GRANT` only to
  `grafomem_rt`. **Must be applied as a superuser/BYPASSRLS role** so the function owner is RLS-exempt
  (FORCE RLS subjects even the table owner) — apply via the superuser public-proxy DSN, same path as
  `ops/rls_decision_hitl.sql`, **not** the app's `grafomem_rt` migration connection.
- `hitl_routes.py` — `attest_request`, `get_request`, `list_approver_requests` each resolve the tenant
  via the function, `SELECT set_config('app.current_tenant', <resolved>, false)`, then run their
  existing lookups under the correct RLS. The approver-registration + signature checks are unchanged —
  they remain the real authorization gate. A wrong resolution still fail-closes on the real row fetch.
  The inbox fetches per-resolved-tenant (RLS scopes to one tenant per GUC) then merges/sorts/caps.

## Test

`tests/test_hitl_attest_rls.py` — drives the **real** self-authenticated endpoints under FORCE RLS as
a self-provisioned NOSUPERUSER NOBYPASSRLS role (not mocked, not token-auth). Asserts the raw
`WHERE request_id` lookup under `default_namespace` returns 0 rows (the pre-fix failure), then that
fetch → 200, inbox → populated, a forged self-approve → 401, and a genuine approver signature → 200 +
`execute_approved_action`. Skips unless the current role is a superuser (needed to own the
RLS-bypassing functions + provision the restricted role); runs in CI.

## Ship / deploy

branch → PR → CI (incl. the new real-path RLS test) → Cowork review of the SECURITY DEFINER surface +
endpoint changes → merge → **apply `ops/hitl_tenant_resolvers.sql` as superuser** → deploy → re-verify
the Phase-2 governed send approve path (same endpoints, also down since the flip) → resume the
graduation gate at Step 5 (test tenant `devtest-track2` `1e5d30a0…` + the approver row are still set up).
