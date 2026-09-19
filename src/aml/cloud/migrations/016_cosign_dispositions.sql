-- class: ledger
-- Migration 016: cosign_dispositions — the append-only log of cgr.disposition.v1 records.
--
-- A cgr.disposition.v1 record (cgr.cosign.v1 envelope; see docs/cgr/cgr-cosign-v1-spec.md,
-- decisions 0009/0010/0011; profile at docs/cgr/cosign-profile-registry.json) is a signed
-- two-party human disposition — append-only evidence, never mutated in place. Hence a
-- ledger-class table (operator decision 3): the ledger role appends+reads, the runtime role
-- reads only, migrate owns. The privilege statements below are the machine-readable authority;
-- the rationale lives in the decision record, not in prose here (the validator scans comments).
--
-- record_nonce replay (spec §4): the issuer is the single pinned runtime key, so uniqueness per
-- (approver_key_id, record_nonce) enforces the spec's per-(issuer, approver_key_id) rule.

CREATE TABLE IF NOT EXISTS cosign_dispositions (
    record_id          TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    profile            TEXT NOT NULL,
    approval_mode      TEXT NOT NULL,
    content_body       JSONB NOT NULL,
    content_digest     TEXT NOT NULL,
    approval_assertion JSONB NOT NULL,
    approver_signature TEXT NOT NULL,
    approver_key_id    TEXT NOT NULL,
    record_nonce       TEXT NOT NULL,
    system_metadata    JSONB NOT NULL,
    system_signature   TEXT NOT NULL,
    issuer_key_id      TEXT NOT NULL,
    assurance          TEXT NOT NULL DEFAULT 'none',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (approver_key_id, record_nonce)
);
CREATE INDEX IF NOT EXISTS idx_cosign_dispositions_tenant ON cosign_dispositions (tenant_id);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_ledger') THEN
    GRANT SELECT, INSERT ON cosign_dispositions TO grafomem_ledger;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    GRANT SELECT ON cosign_dispositions TO grafomem_rt;
    REVOKE INSERT, UPDATE, DELETE ON cosign_dispositions FROM grafomem_rt;
  END IF;
END $$;

-- Approver assurance tier (gap 3a; cosign spec section 10) lives on the enrolment, not the posted
-- record, so it cannot be self-claimed. Default 'none'; a later enrolment path sets other tiers.
ALTER TABLE hitl_approvers ADD COLUMN IF NOT EXISTS assurance TEXT NOT NULL DEFAULT 'none';
