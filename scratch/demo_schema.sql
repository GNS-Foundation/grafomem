-- Demo Schema for W9 Erasure Gap Closure
-- This schema omits ON DELETE CASCADE and introduces erasure_pending.

CREATE TABLE IF NOT EXISTS demo_memories (
    ref           BIGSERIAL PRIMARY KEY,
    content       TEXT NOT NULL,
    written_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from    TIMESTAMPTZ,
    valid_until   TIMESTAMPTZ,
    tenant_id     TEXT,
    superseded_by BIGINT REFERENCES demo_memories(ref),
    written_by    TEXT,
    signature     BYTEA,
    public_key    BYTEA,
    content_enc   TEXT,
    metadata_enc  TEXT
);

-- Note: No ON DELETE CASCADE here!
-- And we add erasure_pending
CREATE TABLE IF NOT EXISTS demo_memory_embeddings (
    ref             BIGINT PRIMARY KEY,
    embedding       vector(1536), -- hardcoding dim for demo
    tenant_id       TEXT NOT NULL DEFAULT '',
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    valid_until     TIMESTAMPTZ NOT NULL DEFAULT '9999-12-31T23:59:59Z',
    erasure_pending TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS demo_idx_mem_tenant_valid
    ON demo_memories(tenant_id, valid_until, valid_from);

CREATE INDEX IF NOT EXISTS demo_idx_emb_tenant
    ON demo_memory_embeddings(tenant_id, valid_until, valid_from);

-- We omit HNSW index in demo for simplicity.

-- Erasure Certificates table mapping the new Coverage field.
CREATE TABLE IF NOT EXISTS demo_erasure_certificates (
    certificate_id          TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL,
    fact_ref                INTEGER NOT NULL,
    fact_content_hash       TEXT,

    -- Coverage explicitly enumerated as JSONB
    coverage                JSONB NOT NULL DEFAULT '{}'::jsonb,
    scrubbed_decision_ids   JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Timing
    erasure_requested_at    TIMESTAMPTZ NOT NULL,
    erasure_completed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Legal basis
    legal_basis             TEXT NOT NULL DEFAULT 'GDPR Article 17 — Right to Erasure',
    requested_by            TEXT,

    -- Provenance
    signature               BYTEA,
    public_key              BYTEA,

    -- Verification
    verified                BOOLEAN NOT NULL DEFAULT FALSE,
    verification_note       TEXT
);
