-- Manifold Phase-0.5 — decision_embeddings VAULT (capability-content vectors for governed decisions).
--
-- Gives CGR real geometry: a 384-d embedding of the decrypted+redacted capability content of each
-- CGR-attributed decision, keyed by (tenant_id, decision_id), joinable decision→outcome→domain.
--
-- VAULT-ONLY posture (encrypted-tier PII-derived — a projection of decrypted decision content):
--   * RLS ENABLE + FORCE + tenant_isolation policy (enforces even the table owner);
--   * EXECUTE/DML granted ONLY to grafomem_rt (the restricted runtime role);
--   * never serialized by any API (no route/exporter returns the `embedding`);
--   * erasure-swept: FK ON DELETE CASCADE from decision_records (hard-delete) + `erasure_pending`
--     + the erasure sweeper (crypto-shred / mark path) — mirrors memory_embeddings.
--
-- MUST be applied AS A SUPERUSER (owner ≠ grafomem_rt so FORCE RLS actually enforces for the app
-- role) — same manual/attested path as ops/rls_decision_hitl.sql + ops/hitl_tenant_resolvers.sql,
-- NOT the grafomem_rt auto-migration connection (which lacks CREATE on schema public).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.decision_embeddings (
    tenant_id       text        NOT NULL,
    decision_id     text        NOT NULL,
    embedding       vector(384) NOT NULL,
    tokenizer_id    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    valid_from      timestamptz NOT NULL DEFAULT now(),
    valid_until     timestamptz,
    erasure_pending timestamptz,
    PRIMARY KEY (tenant_id, decision_id),
    CONSTRAINT decision_embeddings_decision_fk
        FOREIGN KEY (decision_id) REFERENCES public.decision_records(decision_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_decision_embeddings_tenant
    ON public.decision_embeddings(tenant_id);
CREATE INDEX IF NOT EXISTS idx_decision_embeddings_hnsw
    ON public.decision_embeddings USING hnsw (embedding vector_cosine_ops);

ALTER TABLE public.decision_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.decision_embeddings FORCE  ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY tenant_isolation_decision_embeddings ON public.decision_embeddings
    USING      (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
EXCEPTION WHEN duplicate_object THEN null; END $$;

REVOKE ALL ON public.decision_embeddings FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.decision_embeddings TO grafomem_rt;
