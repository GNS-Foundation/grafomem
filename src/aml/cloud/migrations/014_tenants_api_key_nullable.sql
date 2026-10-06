-- Migration 014: make tenants.api_key nullable (014 step a).
--
-- tenants.api_key is no longer a credential (the auth fallback was removed in #160) and
-- no longer a display source (014 step a: /v1/portal/me shows tenant_api_keys metadata).
-- The writers (create_tenant, portal signup, sso) stop populating it, so the NOT NULL
-- constraint must go first or their INSERTs (which omit the column) would fail.
-- Idempotent: DROP NOT NULL on an already-nullable column is a no-op.
-- (015 nulls the residual values; the HELD migration 019 drops the column — B8.)
-- B8 guard: a database whose schema was created after B8 never has the column (ensure_schema no
-- longer creates it), so this step is a no-op there instead of an error. Same effect as before on
-- every database that has the column.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'tenants' AND column_name = 'api_key') THEN
    ALTER TABLE tenants ALTER COLUMN api_key DROP NOT NULL;
  ELSE
    RAISE NOTICE 'migration 014: tenants.api_key absent (post-B8 schema) — nothing to do';
  END IF;
END $$;
