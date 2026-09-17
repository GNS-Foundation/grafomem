-- Migration 012: back-fill free_usage_cache as a migration.
--
-- free_usage_cache was defined only in free_ceiling.py's ensure_schema, gated behind
-- FREE_CEILING_ENABLED (dark), so it is created nowhere at deploy time and the free-tier
-- ceiling cannot be turned on safely (drift-audit prod addendum §1). Create it
-- unconditionally here so enabling the flag needs no schema step. DDL mirrors
-- free_ceiling.py `_SCHEMA_SQL` exactly.
--
-- Grant rule: the runtime role writes this table (the background refresh + the check
-- path), so it needs full DML. Guarded so it is a no-op in single-role self-host.
CREATE TABLE IF NOT EXISTS free_usage_cache (
    tenant_id      TEXT        NOT NULL,
    period_start   TIMESTAMPTZ NOT NULL,
    governed_count BIGINT      NOT NULL DEFAULT 0,
    plan_hint      TEXT,
    refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, period_start)
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON free_usage_cache TO grafomem_rt;
  END IF;
END $$;
