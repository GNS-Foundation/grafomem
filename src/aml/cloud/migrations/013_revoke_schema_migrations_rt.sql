-- Migration 013: the runtime role must not write the migration ledger.
--
-- schema_migrations is written only by the runner (as grafomem_migrate). But
-- ALTER DEFAULT PRIVILEGES grants grafomem_rt full DML on every migrate-created table,
-- so prod AND staging both have grafomem_rt with INSERT/UPDATE/DELETE on
-- schema_migrations (drift-audit prod addendum §3). Revoke the write verbs; keep SELECT.
-- Idempotent (revoking a privilege not held is a no-op); guarded for single-role self-host.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM grafomem_rt;
  END IF;
END $$;
