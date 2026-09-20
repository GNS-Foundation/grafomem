-- Migration 017: add tenant_api_keys.api_key_hash (peppered HMAC-SHA256) — DARK.
--
-- Column only, plus a UNIQUE index. Postgres allows multiple NULLs in a UNIQUE index, so the index
-- coexists with the dark rollout (rows are NULL until backfilled). The backfill is RUNNER-SIDE
-- (scripts/backfill_api_key_hash.py): the pepper is computed in the runner process and NEVER passed
-- into SQL, so it cannot surface in pg_stat_statements, query logs, or error text. Auth still
-- resolves by plaintext (api_key) until the dual-read migration; nothing here reads api_key_hash.
--
-- No CREATE TABLE, so the split-role grant rule is a no-op; the runtime role's existing table-level
-- grants on tenant_api_keys already cover the new column (Postgres extends table SELECT/DML to it).
-- Guarded so a prod that already has the column (via ensure_schema) is a recorded no-op.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'tenant_api_keys' AND column_name = 'api_key_hash'
  ) THEN
    ALTER TABLE tenant_api_keys ADD COLUMN api_key_hash BYTEA;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_api_keys_api_key_hash
  ON tenant_api_keys (api_key_hash);
