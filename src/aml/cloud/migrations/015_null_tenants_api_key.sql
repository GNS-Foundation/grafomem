-- Migration 015: null residual tenants.api_key values.
--
-- 014 stopped writing and reading tenants.api_key (the key lives in tenant_api_keys) and made
-- the column nullable; 016 will DROP the column. This step nulls any residual plaintext value
-- still sitting in the column so no credential-shaped string lingers between 014 and 016.
--
-- IRREVERSIBLE except by re-mint: the plaintext is discarded here. The authoritative key is in
-- tenant_api_keys and is NOT touched — nothing depends on tenants.api_key after 014, so a nulled
-- value is only recoverable by minting a new key (rotate), never from this column.
--
-- Idempotent (a second run finds nothing non-null). No table CREATE ⇒ no grant-rule impact.
-- Logs the before/after non-null count via RAISE NOTICE (visible in the runner output).
-- B8 guard: a database whose schema was created after B8 never has the column (ensure_schema no
-- longer creates it; the HELD migration 019 drops it elsewhere), so this step is a no-op there
-- instead of an error. Same effect as before on every database that has the column.
DO $$
DECLARE
  before_n int;
  after_n  int;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema() AND table_name = 'tenants' AND column_name = 'api_key') THEN
    RAISE NOTICE 'migration 015: tenants.api_key absent (post-B8 schema) — nothing to do';
    RETURN;
  END IF;
  SELECT count(*) INTO before_n FROM tenants WHERE api_key IS NOT NULL;
  RAISE NOTICE 'migration 015: nulling % residual tenants.api_key value(s)', before_n;
  UPDATE tenants SET api_key = NULL WHERE api_key IS NOT NULL;
  SELECT count(*) INTO after_n FROM tenants WHERE api_key IS NOT NULL;
  RAISE NOTICE 'migration 015: tenants.api_key non-null count is now % (was %)', after_n, before_n;
END $$;
