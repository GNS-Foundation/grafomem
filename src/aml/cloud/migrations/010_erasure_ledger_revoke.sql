-- class: ledger
-- Migration 010: make erasure_ledger read-only for the runtime role.
--
-- 009 creates the ledger and grants grafomem_rt SELECT, but ALTER DEFAULT PRIVILEGES
-- also grants the runtime role full DML on every migrate-created table — so on a fresh
-- split-role install grafomem_rt would hold INSERT/UPDATE/DELETE on the append-only
-- ledger. This REVOKE converges that install to rt-read-only WITHOUT an operator step.
-- (009 is grandfathered from the in-file REVOKE rule; this migration supplies it.)
-- Idempotent — revoking a privilege the role does not hold is a no-op; guarded so it is
-- a no-op in single-role self-host where grafomem_rt does not exist. No SQL change to 009.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    REVOKE INSERT, UPDATE, DELETE ON erasure_ledger FROM grafomem_rt;
  END IF;
END $$;
