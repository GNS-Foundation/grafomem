-- B2b Gate-1 — per-tenant operating-point config (τ, K). The gate is OFF for any
-- tenant without an enabled row here (engine resolves review_gate=None ⇒ byte-identical
-- v1 scoring). NOT applied yet — attested, populated only alongside agent_calibration.
-- RLS ENABLE + FORCE + WITH CHECK, same tenant-isolation as every scoped table.

CREATE TABLE IF NOT EXISTS cgr_gate_config (
    tenant_id  TEXT PRIMARY KEY,
    tau        DOUBLE PRECISION NOT NULL,   -- soft-ramp threshold (B2b op-point: 0.10)
    cap_k      DOUBLE PRECISION NOT NULL,   -- per-(source,target) pseudo-count cap (3)
    enabled    BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE cgr_gate_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE cgr_gate_config FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON cgr_gate_config;
CREATE POLICY tenant_isolation ON cgr_gate_config
    USING      (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
GRANT SELECT, INSERT, UPDATE, DELETE ON cgr_gate_config TO grafomem_rt;
