-- Migration 022: data-subject scope and per-data-subject keys (GMP v0.2 §7.7; ADR-0019).
--
-- Three additive changes, all idempotent:
--   1. memories.data_subject_id — the data subject (GDPR sense) a fact is about; NULL = unscoped.
--      `memories` is created by the backend (PostgresGMPBackend._ensure_schema), so this is guarded on
--      the table existing; the backend's ensure_schema adds the same column and index (as 020 did).
--   2. data_subject_deks — RANDOM per-data-subject DEKs wrapped under the tenant DEK (F2), with the
--      key's public fingerprint kept after destruction so a certificate's `data_subject_key:
--      destroyed:<fp>` can be checked against the tombstone (F5). Written by the key manager on the
--      KEY-STORE URL, which from 0019 is the ledger role (F4) — so `grafomem_ledger` gets the DML and
--      the runtime role gets SELECT only (the grant rule needs a runtime grant; SELECT keeps it
--      unable to create or destroy keys). NOT ledger-class: a tombstone is an UPDATE.
--   3. tenants.data_subject_encryption — the per-tenant opt-in (F7). Default FALSE: nothing changes
--      for any tenant until the operator or the tenant turns it on; rows written before that stay
--      tenant-keyed and are deletion-only on erasure (the certificate says so per fact).
--
-- tenant_deks (created by TenantKeyManager.ensure_schema, pre-dates the numbered-migration rule) also
-- gets the ledger role's DML here, because the key manager now connects as that role. The runtime
-- role's DML on tenant_deks is NOT revoked in this migration: the pre-deploy runner applies 022 before
-- the new code is live, and the 0.5.0 process still reaches tenant_deks as grafomem_rt until cutover.
-- A later held migration revokes it once no process uses the old path.
--
-- REVERSIBLE: DROP INDEX IF EXISTS idx_mem_tenant_data_subject; ALTER TABLE memories DROP COLUMN IF
-- EXISTS data_subject_id; DROP TABLE IF EXISTS data_subject_deks; ALTER TABLE tenants DROP COLUMN IF
-- EXISTS data_subject_encryption. Dropping data_subject_deks destroys every data-subject key: every
-- `gfm.ds1:` row becomes unreadable. That is crypto-erasure of all opted-in subjects, not a rollback.

DO $$
BEGIN
    IF to_regclass('memories') IS NOT NULL THEN
        ALTER TABLE memories ADD COLUMN IF NOT EXISTS data_subject_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_mem_tenant_data_subject ON memories(tenant_id, data_subject_id);
    END IF;
    IF to_regclass('tenants') IS NOT NULL THEN
        ALTER TABLE tenants ADD COLUMN IF NOT EXISTS data_subject_encryption BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS data_subject_deks (
    tenant_id        TEXT        NOT NULL,
    data_subject_id  TEXT        NOT NULL,
    wrapped_dek      TEXT        NOT NULL,          -- Fernet(tenant DEK).encrypt(raw DEK); 'DESTROYED' after erasure
    key_fingerprint  TEXT        NOT NULL,          -- sha256(raw DEK)[:32]; survives destruction (certificate check)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    destroyed_at     TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, data_subject_id)
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_ledger') THEN
    GRANT SELECT, INSERT, UPDATE ON data_subject_deks TO grafomem_ledger;
    IF to_regclass('tenant_deks') IS NOT NULL THEN
      GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_deks TO grafomem_ledger;
    END IF;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    GRANT SELECT ON data_subject_deks TO grafomem_rt;
    REVOKE INSERT, UPDATE, DELETE ON data_subject_deks FROM grafomem_rt;
  END IF;
END $$;
