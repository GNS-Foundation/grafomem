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
