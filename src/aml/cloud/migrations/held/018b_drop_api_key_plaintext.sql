-- Migration 018b (HELD): drop tenant_api_keys.api_key (plaintext) and its index — hash-at-rest PR 5,
-- step 2 of 2. IRREVERSIBLE: once applied, the plaintext exists only in backups.
--
-- This file lives in migrations/held/ ON PURPOSE. The runner's ordinary pass applies only
-- migrations/*.sql (non-recursive), so this migration can never run as part of a deploy. It is
-- applied explicitly, once, by an operator:
--
--   python -m aml.cloud.migrations_runner --apply-held 018b_drop_api_key_plaintext.sql --confirm-irreversible
--
-- and the runner refuses unless, on the target database: 018a is recorded as applied; the plaintext
-- column still exists; and NO tenant_api_keys row has api_key_hash IS NULL (such a key could never
-- authenticate again). Operational preconditions (not enforced in SQL): a staging run first, and a
-- fresh backup of the target database taken immediately before.
--
-- Guarded/idempotent: IF EXISTS makes a re-run a no-op. No CREATE TABLE, so the split-role grant
-- rule is a no-op.
DROP INDEX IF EXISTS idx_tenant_api_keys_key;                 -- the plaintext lookup index
ALTER TABLE tenant_api_keys DROP COLUMN IF EXISTS api_key;    -- the plaintext column
