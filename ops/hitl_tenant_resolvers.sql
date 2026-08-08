-- HITL self-authenticated tenant resolvers (Option A — fix for the attest/fetch/inbox RLS 404).
--
-- WHY. The HITL approver endpoints authenticate the approver by Ed25519 SIGNATURE, not a tenant
-- API key (attest, GET a request, and the approver inbox). The auth middleware therefore pins
-- their context to `default_namespace` (auth.py: /v1/hitl/requests/{id}* and /v1/hitl/approvers/*).
-- Those handlers key their lookups on request_id / approver_id ONLY and rely on RLS for tenant
-- scoping. Under FORCE ROW LEVEL SECURITY on hitl_approval_requests + hitl_approvers, the wrong
-- (default_namespace) context fail-closes to 0 rows → 404 on attest/fetch and an empty/403 inbox.
-- This broke at the Track-1 RLS flip and takes down every prod HITL approval (governed send loop
-- included), not just the governed dev loop.
--
-- FIX (Option A). Resolve the OWNING tenant of a request/approver via a SECURITY DEFINER function
-- that returns ONLY the tenant id(s) — no row data. The handler sets app.current_tenant to the
-- resolved value and then runs its EXISTING logic under the CORRECT tenant's RLS; a wrong
-- resolution still fail-closes on the real row fetch. The approver-registration + signature checks
-- are unchanged — they remain the real authorization gate.
--
-- GUARDRAILS.
--   * Returns only the tenant id(s), never row contents.
--   * SECURITY DEFINER + `SET search_path` (no search_path injection); tables schema-qualified.
--   * REVOKE EXECUTE FROM PUBLIC; GRANT only to the runtime role grafomem_rt.
--   * MUST be applied as a SUPERUSER / BYPASSRLS role so the function OWNER is exempt from RLS —
--     FORCE RLS subjects even the table owner, so SECURITY DEFINER only bypasses when its owner
--     itself bypasses RLS. Apply via the superuser public-proxy DSN (same path as
--     ops/rls_decision_hitl.sql), NOT the app's grafomem_rt migration connection.

CREATE OR REPLACE FUNCTION public.hitl_request_tenant(p_request_id text)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT tenant_id FROM public.hitl_approval_requests WHERE request_id = p_request_id;
$$;

CREATE OR REPLACE FUNCTION public.hitl_approver_tenants(p_approver_id text)
RETURNS text[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT array_agg(tenant_id) FROM public.hitl_approvers
    WHERE approver_id = p_approver_id AND active = TRUE;
$$;

REVOKE EXECUTE ON FUNCTION public.hitl_request_tenant(text)   FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.hitl_approver_tenants(text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION public.hitl_request_tenant(text)   TO grafomem_rt;
GRANT  EXECUTE ON FUNCTION public.hitl_approver_tenants(text) TO grafomem_rt;
