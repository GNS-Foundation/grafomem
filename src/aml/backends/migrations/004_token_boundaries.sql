-- Phase 9: Context Infrastructure & Prefix Topography
-- Adds optional token boundaries to memory embeddings for O(1) context packing.

ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS token_count INTEGER;
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS tokenizer_id TEXT;
