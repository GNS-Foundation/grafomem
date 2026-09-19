-- Migration 005: HITL Approvers and Requests

CREATE TABLE IF NOT EXISTS hitl_approvers (
    approver_id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    public_key VARCHAR NOT NULL,
    role VARCHAR,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hitl_approval_requests (
    request_id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    workflow_id VARCHAR NOT NULL,
    step_id VARCHAR,
    action VARCHAR,
    resource VARCHAR,
    context_json JSONB,
    context_bytes BYTEA NOT NULL,
    nonce VARCHAR NOT NULL,
    issued_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending', -- pending, approved, denied, expired
    signer_id VARCHAR,
    signature VARCHAR,
    decided_at TIMESTAMP WITH TIME ZONE
);

-- Runtime role receives DML in split-role deployments: the runtime manages approver
-- enrolment and the approval-request lifecycle. Guarded so it is a no-op in single-role
-- self-host (grafomem_rt absent). Added 2026-09-19 for the split-role rule (these tables
-- predate it); already-applied environments skip this file.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON hitl_approvers TO grafomem_rt;
    GRANT SELECT, INSERT, UPDATE, DELETE ON hitl_approval_requests TO grafomem_rt;
  END IF;
END $$;
