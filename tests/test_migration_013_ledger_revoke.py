"""A2 carve-out (a): migration 013 negative probe.

013 revokes INSERT/UPDATE/DELETE on schema_migrations from the runtime role grafomem_rt (which
ALTER DEFAULT PRIVILEGES would otherwise grant), keeping SELECT — so the runtime role cannot
forge the migration ledger. This test pins that property against the REAL 013 SQL: it sets up
the pre-013 state (rt with full DML), applies 013, and asserts write is gone while read remains
— both via the privilege catalog AND behaviourally with SET ROLE.

Needs a superuser connection (CREATE ROLE / SET ROLE); skips otherwise. In CI the Postgres
service user is a superuser, so this runs. Locally point GRAFOMEM_DB_URL at a superuser role to
exercise it (the default 'grafomem' dev role is not a superuser → skip).
"""
import os
import pathlib

import psycopg
import pytest

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
MIG_013 = pathlib.Path(__file__).resolve().parents[1] / \
    "src/aml/cloud/migrations/013_revoke_schema_migrations_rt.sql"


def _priv(conn, priv: str) -> bool:
    return conn.execute(
        "SELECT has_table_privilege('grafomem_rt', 'schema_migrations', %s)", (priv,)
    ).fetchone()[0]


def test_013_runtime_role_cannot_write_schema_migrations():
    try:
        conn = psycopg.connect(DB_URL, autocommit=True, connect_timeout=5)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no database: {e}")
    with conn:
        su = conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        if not su or not su[0]:
            pytest.skip("needs a superuser connection (CREATE ROLE / SET ROLE)")

        conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grafomem_rt') "
            "THEN CREATE ROLE grafomem_rt NOLOGIN; END IF; END $$;")
        # The runtime role has USAGE on public in a real split-role deploy; grant it so the
        # behavioural probe below hits the TABLE privilege (permission denied) rather than a
        # name-resolution error (relation does not exist).
        conn.execute("GRANT USAGE ON SCHEMA public TO grafomem_rt")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations "
            "(version TEXT PRIMARY KEY, applied_via TEXT, applied_at TIMESTAMPTZ DEFAULT now())")

        # Pre-013 state: the runtime role has full DML (as ALTER DEFAULT PRIVILEGES grants).
        conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON schema_migrations TO grafomem_rt")
        assert _priv(conn, "INSERT") is True, "setup: rt should have INSERT before 013"

        # Apply the REAL migration 013.
        conn.execute(MIG_013.read_text())

        # After 013: write revoked, read kept.
        assert _priv(conn, "INSERT") is False, "013 must REVOKE INSERT from grafomem_rt"
        assert _priv(conn, "UPDATE") is False, "013 must REVOKE UPDATE from grafomem_rt"
        assert _priv(conn, "DELETE") is False, "013 must REVOKE DELETE from grafomem_rt"
        assert _priv(conn, "SELECT") is True, "013 must KEEP SELECT for grafomem_rt"

        # Behavioural: as grafomem_rt, the write actually raises and the read still works.
        conn.execute("SET ROLE grafomem_rt")
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO public.schema_migrations (version, applied_via) VALUES ('probe-013', 'test')")
            conn.execute("SELECT 1 FROM public.schema_migrations LIMIT 1").fetchone()  # read still permitted
        finally:
            conn.execute("RESET ROLE")
