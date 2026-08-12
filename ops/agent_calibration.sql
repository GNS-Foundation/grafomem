-- B2b Gate-1 — agent_calibration table (per-tenant source calibration weight `w`).
-- APPLIED to prod 2026-08-12 (superuser, Camilo-attested). Kept here for reproducibility.
--
-- `calibration_weight` (w ∈ [0,1]) gates the CGR REVIEW channel via cgr/gate.py:
-- g(w)=max(0,(w−τ)/(1−τ)); NULL/absent ⇒ g=0 (cold-start fail-safe). Never gates the
-- verifiable channel. RLS ENABLE + FORCE + WITH CHECK, same as every tenant-scoped table.
--
-- WRITE AUTHORITY (enforced on the write path, not by this table): `calibration_weight`
-- is writable ONLY by the identity authority (sim operator / GEIANT) holding the
-- privileged `calibration:write` scope — NEVER an agent's own ingestion key. A
-- self-assignable w defeats the gate. Populating w is held until that path is reviewed.

CREATE TABLE IF NOT EXISTS agent_calibration (
    tenant_id          TEXT NOT NULL,
    agent_key          TEXT NOT NULL,            -- GEIANT Ed25519 pubkey (hex)
    calibration_weight DOUBLE PRECISION,         -- w ∈ [0,1]; NULL/absent ⇒ g(w)=0 (fail-safe)
    n_observations     INTEGER NOT NULL DEFAULT 0, -- calibration PROVENANCE — distinct from CGRResult.n_resolved
    method             TEXT,                     -- how w was measured (provenance)
    as_of              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, agent_key)
);

ALTER TABLE agent_calibration ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_calibration FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON agent_calibration;
CREATE POLICY tenant_isolation ON agent_calibration
    USING      (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON agent_calibration TO grafomem_rt;
