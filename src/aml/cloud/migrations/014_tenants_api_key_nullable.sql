-- Migration 015: make tenants.api_key nullable (014 step a).
--
-- tenants.api_key is no longer a credential (the auth fallback was removed in #160) and
-- no longer a display source (014 step a: /v1/portal/me shows tenant_api_keys metadata).
-- The writers (create_tenant, portal signup, sso) stop populating it, so the NOT NULL
-- constraint must go first or their INSERTs (which omit the column) would fail.
-- Idempotent: DROP NOT NULL on an already-nullable column is a no-op.
-- (015 nulls the residual values, 016 drops the column — later steps.)
ALTER TABLE tenants ALTER COLUMN api_key DROP NOT NULL;
