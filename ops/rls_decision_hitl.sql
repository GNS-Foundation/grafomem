-- ============================================================================
-- Track 1 — Postgres RLS enforcement for decision_records + HITL tables
-- ============================================================================
-- DB backstop for the two tenant-isolation bugs dogfooding found in hitl_routes
-- (list_requests tenant_id=NULL; verify_request cross-tenant IDOR). Grounds in the
-- #12 memories pattern (app.current_tenant + tenant_isolation policy) and adds the
-- FORCE + non-superuser runtime role the #12 comment deferred.
--
-- STEP-0 LIVE STATE (2026-08-07, prod via public proxy):
--   role: app connects as `postgres` (rolsuper=t, rolbypassrls=t, table owner) → RLS INERT.
--   grafomem_rt: DOES NOT EXIST yet.
--   memories:               rls=ENABLED force=NO  policy=tenant_isolation_memories (inert: postgres bypasses)
--   decision_records:       rls=NO      force=NO  policy=NONE
--   hitl_approval_requests: rls=NO      force=NO  policy=NONE
--   hitl_approvers:         rls=NO      force=NO  policy=NONE
--   (all three have a tenant_id column — verified.)
--
-- MANUAL, POST-REVIEW prod apply. Idempotent. Run as the table OWNER (postgres).
-- This file is APPLY PHASE A/B only; the enforcement SWITCH is Phase C (repoint the
-- app's DB user to grafomem_rt — a Railway env change), done LAST, AFTER the code half
-- (app.current_tenant on 100% of connections) is deployed. Applying A/B alone changes
-- NOTHING for the running app (postgres bypasses RLS), so it is safe to land early.
-- ============================================================================

-- ── PHASE A — restricted runtime role (the "non-superuser so FORCE applies" half) ──
-- grafomem_rt: LOGIN, NOSUPERUSER, NOBYPASSRLS, and NOT an owner of any table.
-- Under this role RLS is enforced (a non-owner is subject to RLS even without FORCE;
-- FORCE below additionally covers the owner path). Password is set OUT OF BAND by the
-- DB admin (never in git): ALTER ROLE grafomem_rt PASSWORD '<from secret manager>';
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    CREATE ROLE grafomem_rt LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END $$;
-- Idempotently assert the security-critical attributes even if it pre-existed:
ALTER ROLE grafomem_rt NOSUPERUSER NOBYPASSRLS;

-- Grants. The app repoints WHOLLY to grafomem_rt, so it needs DML on every table it
-- touches + sequence usage. RLS filters rows; grants gate the operation. NO ownership,
-- NO DDL (ensure_schema is gated off under this role — schema changes run as owner).
GRANT USAGE ON SCHEMA public TO grafomem_rt;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO grafomem_rt;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO grafomem_rt;
-- Future tables/sequences (if the owner creates any) inherit the same grants:
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO grafomem_rt;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO grafomem_rt;

-- ── PHASE B — enable + FORCE RLS + tenant-isolation policies (still inert under postgres) ──
-- FORCE: applies RLS even to the table owner. Belt-and-suspenders — the real enforcement
-- is grafomem_rt being non-superuser/non-bypassrls; FORCE guards against a future where
-- the connecting role owns the table. (NB: neither FORCE nor policies stop a SUPERUSER or
-- BYPASSRLS role — that is why Phase C repoints off postgres.)

-- decision_records ----------------------------------------------------------
ALTER TABLE decision_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_records FORCE  ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY tenant_isolation_decision_records ON decision_records
    USING      (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
EXCEPTION WHEN duplicate_object THEN null; END $$;

-- hitl_approval_requests ----------------------------------------------------
ALTER TABLE hitl_approval_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE hitl_approval_requests FORCE  ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY tenant_isolation_hitl_requests ON hitl_approval_requests
    USING      (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
EXCEPTION WHEN duplicate_object THEN null; END $$;

-- hitl_approvers ------------------------------------------------------------
ALTER TABLE hitl_approvers ENABLE ROW LEVEL SECURITY;
ALTER TABLE hitl_approvers FORCE  ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY tenant_isolation_hitl_approvers ON hitl_approvers
    USING      (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
EXCEPTION WHEN duplicate_object THEN null; END $$;

-- Uniformity with the FORCE ask: bring the already-enabled #12 tables up to FORCE too.
-- (Safe now — inert under postgres; matters once repointed.)
ALTER TABLE memories          FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_embeddings FORCE ROW LEVEL SECURITY;

-- (review #2) Close the #12 'admin' OR-clause on memories/embeddings so the WHOLE policied
-- surface is uniformly no-bypass before Phase C activates memories RLS. Replace the existing
-- admin-carrying policies (prod already has them) with clean tenant-only ones.
DROP POLICY IF EXISTS tenant_isolation_memories ON memories;
CREATE POLICY tenant_isolation_memories ON memories
  USING (tenant_id = current_setting('app.current_tenant', true));
DROP POLICY IF EXISTS tenant_isolation_embeddings ON memory_embeddings;
CREATE POLICY tenant_isolation_embeddings ON memory_embeddings
  USING (tenant_id = current_setting('app.current_tenant', true));

-- ── VERIFY (read-only; run after apply) ──
-- SELECT c.relname, c.relrowsecurity AS rls, c.relforcerowsecurity AS force
--   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
--   WHERE n.nspname='public'
--     AND c.relname IN ('decision_records','hitl_approval_requests','hitl_approvers','memories','memory_embeddings');
-- SELECT tablename, policyname, cmd FROM pg_policies WHERE schemaname='public'
--   AND tablename IN ('decision_records','hitl_approval_requests','hitl_approvers');
-- SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname='grafomem_rt';   -- want f, f

-- ── PHASE C (NOT in this file — the enforcement switch) ──
-- After the code half is deployed (app.current_tenant set on every connection touching
-- these tables) AND Phase A/B applied: repoint the app's DB user to grafomem_rt by
-- changing GRAFOMEM_DB_URL's username/password in Railway (backend + daemon services).
-- Rollback = repoint back to postgres (instant, no data change).
