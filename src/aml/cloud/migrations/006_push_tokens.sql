-- Migration 006: approver push tokens.
--
-- AMENDED IN PLACE 2026-09-15 (one-time exception to migration immutability — this
-- file had never been applied in any environment we control; see migrations/README.md).
-- Adds tenant scoping, an approver FK with cascade (push tokens are erased with their
-- approver), and the runtime-role GRANT the release runner's grant rule requires.
-- External self-hosters who applied the ORIGINAL 006 converge via 008.

CREATE TABLE IF NOT EXISTS approver_push_tokens (
    approver_id TEXT NOT NULL REFERENCES hitl_approvers(approver_id) ON DELETE CASCADE,
    tenant_id   TEXT NOT NULL,
    platform    TEXT NOT NULL,
    push_token  TEXT NOT NULL,
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc', now()) NOT NULL,
    UNIQUE(approver_id, push_token)
);

CREATE INDEX IF NOT EXISTS idx_approver_push_tokens_approver_id ON approver_push_tokens(approver_id);
CREATE INDEX IF NOT EXISTS idx_approver_push_tokens_tenant_id   ON approver_push_tokens(tenant_id);

-- Runtime role gets DML (split-role deployments). Guarded so it is a no-op in
-- single-role self-host, where grafomem_rt does not exist.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON approver_push_tokens TO grafomem_rt;
  END IF;
END $$;
