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
DO $$
DECLARE
  before_n int;
  after_n  int;
BEGIN
  SELECT count(*) INTO before_n FROM tenants WHERE api_key IS NOT NULL;
  RAISE NOTICE 'migration 015: nulling % residual tenants.api_key value(s)', before_n;
  UPDATE tenants SET api_key = NULL WHERE api_key IS NOT NULL;
  SELECT count(*) INTO after_n FROM tenants WHERE api_key IS NOT NULL;
  RAISE NOTICE 'migration 015: tenants.api_key non-null count is now % (was %)', after_n, before_n;
END $$;
