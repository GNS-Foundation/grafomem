-- Sprint 23 & 24 Migrations
-- This file contains all columns added across the Enterprise and Encryption sprints.

-- 1. Tenant Manager (Sprint 23 - Enterprise Security)
ALTER TABLE tenant_api_keys ADD COLUMN IF NOT EXISTS scopes TEXT[] DEFAULT '{}';
ALTER TABLE tenant_api_keys ADD COLUMN IF NOT EXISTS allowed_stores TEXT[] DEFAULT '{}';
ALTER TABLE tenant_api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE tenant_api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
ALTER TABLE tenant_api_keys ADD COLUMN IF NOT EXISTS ip_allowlist TEXT[] DEFAULT '{}';
ALTER TABLE tenant_api_keys ADD COLUMN IF NOT EXISTS is_service_account BOOLEAN DEFAULT false;

-- 2. Governance Gateway (Sprint 24 - Encryption/Feature toggles)
ALTER TABLE governance_policies ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE governance_policies ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 100;

-- 3. Decision Trail (Sprint 24 - Encryption)
ALTER TABLE decision_records ADD COLUMN IF NOT EXISTS parent_decision_id TEXT REFERENCES decision_records(decision_id);
ALTER TABLE decision_records ADD COLUMN IF NOT EXISTS query_enc TEXT;
ALTER TABLE decision_records ADD COLUMN IF NOT EXISTS retrieved_contents_enc TEXT;
ALTER TABLE decision_records ADD COLUMN IF NOT EXISTS raw_output_enc TEXT;
ALTER TABLE decision_records ADD COLUMN IF NOT EXISTS parsed_output_enc TEXT;

-- 4. Orchestrator (Sprint 24 - Observability and Tracking)
ALTER TABLE orchestrator_workflows ADD COLUMN IF NOT EXISTS termination_reason TEXT;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS parent_decision_id TEXT;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS is_synthetic BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS latency_governance_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS latency_memory_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS latency_llm_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS latency_tools_ms INTEGER NOT NULL DEFAULT 0;

ALTER TABLE orchestrator_agents ADD COLUMN IF NOT EXISTS fallback_models JSONB NOT NULL DEFAULT '[]';
ALTER TABLE orchestrator_agents ADD COLUMN IF NOT EXISTS system_prompt_enc TEXT;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS input_text_enc TEXT;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS retrieved_facts_enc TEXT;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS governance_logs_enc TEXT;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS raw_output_enc TEXT;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS tool_calls_enc TEXT;
ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS tool_results_enc TEXT;

