-- Migration 009: erasure_ledger — the append-only crypto-erasure log.
--
-- The I0 shape (erasure_ledger.py:ensure_schema) plus the I0c `backfill` provenance
-- column. CREATE IF NOT EXISTS is deliberate: on prod the table already exists (created
-- by the I0 operator step + I0c ALTER), so verify-first baseline records 009 without
-- re-running; on staging / fresh installs it is created here.
--
-- Ledger table ⇒ the grant rule requires grants to BOTH roles: grafomem_ledger (the
-- ledger connection that writes it) and grafomem_rt (the app, read-only). Both grants
-- are guarded so the migration is a no-op in single-role self-host, where neither role
-- exists.

CREATE TABLE IF NOT EXISTS erasure_ledger (
    entry_id     TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    entry_type   TEXT NOT NULL,
    fact_ref     INTEGER,
    content_hash TEXT,
    certificate  JSONB,
    backfill     JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_ledger') THEN
    GRANT SELECT, INSERT ON erasure_ledger TO grafomem_ledger;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    GRANT SELECT ON erasure_ledger TO grafomem_rt;
  END IF;
END $$;
