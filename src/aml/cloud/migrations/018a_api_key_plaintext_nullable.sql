-- Migration 018a: tenant_api_keys.api_key (plaintext) becomes NULLABLE — hash-at-rest PR 5, step 1 of 2.
--
-- Shipped WITH the hash-only code: from this release the server resolves API keys by api_key_hash
-- only and every mint writes api_key NULL. Existing rows keep their plaintext (nothing is read from
-- it any more); the column is dropped later by the HELD migration 018b, after a staging check and a
-- fresh backup, never by the ordinary runner pass.
--
-- REVERSIBLE: `ALTER TABLE tenant_api_keys ALTER COLUMN api_key SET NOT NULL` restores the
-- constraint (after re-populating any NULLs, i.e. only before the hash-only code is deployed).
-- Guarded/idempotent: re-running on a DB whose column is already nullable (or already dropped by
-- 018b) is a no-op. No CREATE TABLE, so the split-role grant rule is a no-op.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'tenant_api_keys' AND column_name = 'api_key' AND is_nullable = 'NO'
  ) THEN
    ALTER TABLE tenant_api_keys ALTER COLUMN api_key DROP NOT NULL;
  END IF;
END $$;
