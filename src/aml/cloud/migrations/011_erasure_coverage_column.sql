-- Migration 011: add erasure_certificates.coverage.
--
-- The `coverage` column was defined ONLY in erasure_proof.py's ensure_schema
-- _SCHEMA_SQL (`coverage JSONB NOT NULL DEFAULT '{}'::jsonb`), never in any
-- migration. Environments provisioned purely by the migration runner (staging)
-- created erasure_certificates via 002_w9_erasure.sql — which has no coverage —
-- and ensure_schema could not add it (the runtime role has no DDL on schema
-- public), so the column is absent and erasure issuance 500s (UndefinedColumn:
-- column "coverage" ... does not exist) even though schema_migrations shows
-- 001-010 applied. Surfaced by the 0014 stage-1 staging E2E.
--
-- Prod already HAS the column: its erasure_certificates was built by
-- ensure_schema while the process ran as postgres, so 011 is a recorded no-op
-- there. The guard below attempts the ALTER only when the column is ABSENT, so
-- on prod nothing is altered — no table ownership is required for the no-op.
-- (ADD COLUMN with an explicit NOT NULL DEFAULT backfills existing rows to '{}',
-- which verify_erasure_effect already treats as a coverage gap.)

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'erasure_certificates' AND column_name = 'coverage'
  ) THEN
    ALTER TABLE erasure_certificates
      ADD COLUMN coverage JSONB NOT NULL DEFAULT '{}'::jsonb;
  END IF;
END $$;
