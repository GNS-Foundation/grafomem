-- 007: CGR identity on orchestrator agents (Phase 2, PR-1).
--
-- The CGR engine groups reputation scores by a stable `agent_key` (a GEIANT Ed25519
-- pubkey, folded to its rotation anchor), falling back to `agent_handle`
-- (cgr/engine.py `_gkey`). Orchestrator agents had neither, so their governed
-- decisions were invisible to CGR. These columns let an orchestrated agent carry the
-- same stable identity used by the /v1/governed/decisions path, so its decisions can
-- be attributed and scored. Nullable + idempotent: existing agents are unaffected
-- until a key is set.
ALTER TABLE orchestrator_agents ADD COLUMN IF NOT EXISTS agent_key TEXT;
ALTER TABLE orchestrator_agents ADD COLUMN IF NOT EXISTS agent_handle TEXT;
