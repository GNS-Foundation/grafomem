-- Migration 008: converge approver_push_tokens for anyone who applied the ORIGINAL
-- 006 (no tenant_id, no approver FK). Fully idempotent; a no-op on installs that
-- received the amended 006. Exists only because 006 was amended in place — see
-- migrations/README.md.

-- tenant_id: nullable here (existing rows predate it; a self-hoster backfills before
-- tightening to NOT NULL). Fresh installs already got NOT NULL via the amended 006.
ALTER TABLE approver_push_tokens ADD COLUMN IF NOT EXISTS tenant_id TEXT;

CREATE INDEX IF NOT EXISTS idx_approver_push_tokens_tenant_id ON approver_push_tokens(tenant_id);

-- FK approver_id -> hitl_approvers ON DELETE CASCADE, added only if absent
-- (PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS). The amended-006 inline FK is
-- auto-named approver_push_tokens_approver_id_fkey, so this is a no-op there.
DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
      WHERE conname = 'approver_push_tokens_approver_id_fkey'
        AND conrelid = 'approver_push_tokens'::regclass
  ) THEN
    ALTER TABLE approver_push_tokens
      ADD CONSTRAINT approver_push_tokens_approver_id_fkey
      FOREIGN KEY (approver_id) REFERENCES hitl_approvers(approver_id) ON DELETE CASCADE;
  END IF;
END $$;

-- Runtime-role GRANT, guarded (no-op in single-role self-host).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON approver_push_tokens TO grafomem_rt;
  END IF;
END $$;
