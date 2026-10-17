-- Migration 020: memories.store_id — the store a memory was written through (0.4.1 scoped audit).
--
-- Every store of a server shares the `memories` table, so the store audit filters on this column (and on the
-- tenant). Memories written before 0.4.1 keep store_id NULL: they are not attributable to a store and a
-- store-scoped audit does not return them (the tenant filter alone still never returns another tenant's rows).
--
-- `memories` is created by the memory backend (PostgresGMPBackend._ensure_schema), not by this runner, so the
-- change is guarded on the table existing; the backend's ensure_schema adds the same column and index.
-- REVERSIBLE: `DROP INDEX IF EXISTS idx_mem_tenant_store; ALTER TABLE memories DROP COLUMN IF EXISTS store_id`.
-- Idempotent. No CREATE TABLE, so the split-role grant rule is a no-op.
DO $$
BEGIN
    IF to_regclass('memories') IS NOT NULL THEN
        ALTER TABLE memories ADD COLUMN IF NOT EXISTS store_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_mem_tenant_store ON memories(tenant_id, store_id);
    END IF;
END $$;
