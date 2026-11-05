-- class: ledger
-- Migration 021: hash-chain erasure_ledger (decision 0017).
--
-- ADDITIVE. Every existing column of every existing row is left untouched. The ledger
-- gains a sequence and a hash chain so that a dropped or reordered row — by a higher
-- privilege, or by restoring an older backup of the ledger itself — becomes detectable
-- (the role grants of 010 only prevent the runtime role from writing; they cannot
-- prove nothing was removed).
--
-- Construction is byte-identical to the LangGraph adapter's receipt_log.py, so one
-- verifier reads both:  entry_hash = sha256(prev_hash || canonical(payload)),
-- canonical = JSON with sorted keys and no whitespace, GENESIS = 64 zeros.
--
-- Legacy rows (present before this migration) get a deterministic seq and keep NULL
-- hashes: their per-row canonical JSON is not reproduced in SQL. Instead ONE sealing
-- row (entry_type chain_genesis) commits to their ordered entry_ids; everything after
-- it is chained row by row. erasure_ledger.verify_ledger() recomputes both.
--
-- No CREATE TABLE here, so the split-role grant rule is not triggered; the ledger role's
-- SELECT+INSERT from 009 already covers the new columns. The one UPDATE below fills a
-- NEW column on legacy rows and runs as the migrate role (owner) — the ledger and
-- runtime roles cannot UPDATE this table (010). Requires PostgreSQL >= 11 (sha256()).
-- Idempotent: re-running adds no column, re-seals nothing, inserts no second genesis.

ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS seq        BIGINT;
ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS prev_hash  TEXT;
ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS entry_hash TEXT;

-- Legacy rows: deterministic seq (created_at, then entry_id as the tie-break).
UPDATE erasure_ledger e
   SET seq = s.rn
  FROM (SELECT entry_id, ROW_NUMBER() OVER (ORDER BY created_at, entry_id) AS rn
          FROM erasure_ledger
         WHERE seq IS NULL) s
 WHERE e.entry_id = s.entry_id;

-- Seal the legacy segment with one genesis row. The payload is
--   {"entry_type":"chain_genesis","legacy_entry_ids":["<id>",...]}
-- in canonical form: keys are already in sorted order, entry_ids are hex strings so no
-- JSON escaping is needed, string_agg(... ORDER BY seq) gives seq order, and an empty
-- ledger yields "[]". The genesis row's prev_hash is GENESIS itself.
INSERT INTO erasure_ledger (entry_id, tenant_id, entry_type, seq, prev_hash, entry_hash, created_at)
SELECT 'chain_genesis',
       '__ledger__',
       'chain_genesis',
       COALESCE(MAX(seq), 0) + 1,
       repeat('0', 64),
       encode(sha256(convert_to(
           repeat('0', 64)
           || '{"entry_type":"chain_genesis","legacy_entry_ids":['
           || COALESCE(string_agg('"' || entry_id || '"', ',' ORDER BY seq), '')
           || ']}',
           'UTF8')), 'hex'),
       now()
  FROM erasure_ledger
-- HAVING, not WHERE: with aggregates, a WHERE would filter the INPUT rows and the
-- aggregate would still emit one (empty) row — inserting a second, wrong genesis.
-- HAVING filters the aggregate's OUTPUT row, which is the idempotency guard we need.
HAVING NOT EXISTS (SELECT 1 FROM erasure_ledger WHERE entry_type = 'chain_genesis');

ALTER TABLE erasure_ledger ALTER COLUMN seq SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS erasure_ledger_seq_key ON erasure_ledger (seq);
