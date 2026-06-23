-- W9 Erasure Architecture Gap Closure
-- Decoupling the embedding lifecycle from the primary memory lifecycle
-- so that embeddings can outlive the primary memory for cryptographic erasure verification.

-- 1. Drop the synchronous cascade dependency
ALTER TABLE IF EXISTS memory_embeddings DROP CONSTRAINT IF EXISTS memory_embeddings_ref_fkey;

-- 2. Introduce the right-to-be-forgotten tracking field
ALTER TABLE IF EXISTS memory_embeddings ADD COLUMN IF NOT EXISTS erasure_pending TIMESTAMPTZ;
