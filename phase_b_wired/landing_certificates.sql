-- landing_certificates.sql  (PHASE-B — table #23)
-- Apply via however you manage schema (the 22 existing tables live in PostgreSQL + pgvector).
-- Follows the JSONB-payload + tenant-scoped pattern; tenant_id filter on every read
-- (the multi-tenant isolation discipline from the §23 fix).

CREATE TABLE IF NOT EXISTS landing_certificates (
  certificate_id     TEXT PRIMARY KEY,                 -- BLAKE2b-128 hex
  tenant_id          TEXT NOT NULL REFERENCES tenants(tenant_id),
  artifact_ref       TEXT NOT NULL,
  base_model_ref     TEXT NOT NULL,
  layer_hashes       JSONB NOT NULL,                   -- [BLAKE2b-256]
  data_provenance    JSONB NOT NULL,                   -- corpus_hash, epoch_id, merkle_root,
                                                        -- source_leaf, inclusion_proof, composition_ref
  authority          JSONB NOT NULL,                   -- delegation_ref, human_principal, trust_tier,
                                                        -- delegation_sig, delegation_signed_body
  conformance        JSONB NOT NULL,                   -- harness_version, result, per_policy
  permitted_actions  JSONB NOT NULL,
  anchor             JSONB,                            -- {mode:'epoch', epoch_id, merkle_root, inclusion_proof, anchor_proof}
                                                        -- or {mode:'chain', receipt_id, previous_receipt_hash}
  status             TEXT NOT NULL DEFAULT 'issued',   -- issued | waiting_hitl | denied | revoked
  signature          TEXT NOT NULL,                    -- Ed25519 hex
  signer_public_key  TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_landing_certs_tenant   ON landing_certificates(tenant_id);
CREATE INDEX IF NOT EXISTS ix_landing_certs_artifact ON landing_certificates(tenant_id, artifact_ref);
CREATE INDEX IF NOT EXISTS ix_landing_certs_status   ON landing_certificates(tenant_id, status);
