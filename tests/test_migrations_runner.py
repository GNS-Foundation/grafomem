"""Migration runner — grant rule, boot gating, packaging. Hermetic (no DB).

The prod finding these pin: in a split-role deployment the runtime role (grafomem_rt)
owns no tables, so a migration that CREATEs a table without GRANTing that role is
useless — and the boot runner must never attempt DDL in cloud mode. Plus the .sql
files must ship in the wheel or the runner silently finds nothing.
"""
import pytest

from aml.cloud.migrations_runner import (
    MigrationError,
    boot_migrations_enabled,
    validate_migration_sql,
    verify_baseline_present,
    _check_ident,
    _sql_files,
    _migrations_dir,
)

RT = "grafomem_rt"


# ── must-fail: CREATE TABLE with no GRANT to the runtime role is rejected ─────
def test_create_without_grant_is_rejected():
    sql = "CREATE TABLE approver_push_tokens (approver_id TEXT NOT NULL);"
    with pytest.raises(MigrationError):
        validate_migration_sql("006_push_tokens.sql", sql, runtime_role=RT)  # must raise


def test_create_with_grant_passes():
    sql = (
        "CREATE TABLE approver_push_tokens (approver_id TEXT NOT NULL);\n"
        "GRANT SELECT, INSERT, UPDATE, DELETE ON approver_push_tokens TO grafomem_rt;"
    )
    validate_migration_sql("006_push_tokens.sql", sql, runtime_role=RT)  # no raise


def test_create_if_not_exists_and_public_schema_are_matched():
    sql = (
        "CREATE TABLE IF NOT EXISTS public.foo (id INT);\n"
        "GRANT SELECT ON public.foo TO grafomem_rt;"
    )
    validate_migration_sql("x.sql", sql, runtime_role=RT)  # no raise
    with pytest.raises(MigrationError):
        validate_migration_sql("x.sql", "CREATE TABLE IF NOT EXISTS public.foo (id INT);", runtime_role=RT)


def test_ledger_table_needs_ledger_grant_too():
    sql = (
        "CREATE TABLE erasure_ledger (entry_id TEXT PRIMARY KEY);\n"
        "GRANT SELECT, INSERT ON erasure_ledger TO grafomem_rt;"
    )
    with pytest.raises(MigrationError):  # missing grant to grafomem_ledger
        validate_migration_sql("00x_ledger.sql", sql, runtime_role=RT)
    sql_ok = sql + "\nGRANT SELECT, INSERT ON erasure_ledger TO grafomem_ledger;"
    validate_migration_sql("00x_ledger.sql", sql_ok, runtime_role=RT)  # no raise


def test_ledger_grant_accepts_combined_two_role_block():
    # A single GRANT naming both roles must satisfy the ledger rule.
    sql = (
        "CREATE TABLE erasure_ledger (entry_id TEXT PRIMARY KEY);\n"
        "GRANT SELECT ON erasure_ledger TO grafomem_rt, grafomem_ledger;"
    )
    validate_migration_sql("00x_ledger.sql", sql, runtime_role=RT)  # no raise


def test_ledger_grant_accepts_guarded_do_block_grants():
    # Grants wrapped in a DO $$ … $$ guard (as migration 009 writes them) are detected.
    sql = (
        "CREATE TABLE IF NOT EXISTS erasure_ledger (entry_id TEXT PRIMARY KEY);\n"
        "DO $$ BEGIN\n"
        "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grafomem_ledger') THEN\n"
        "    GRANT SELECT, INSERT ON erasure_ledger TO grafomem_ledger;\n"
        "  END IF;\n"
        "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grafomem_rt') THEN\n"
        "    GRANT SELECT ON erasure_ledger TO grafomem_rt;\n"
        "  END IF;\n"
        "END $$;"
    )
    validate_migration_sql("009_erasure_ledger.sql", sql, runtime_role=RT)  # no raise


def test_real_009_satisfies_two_role_ledger_grant_rule():
    sql = (_migrations_dir() / "009_erasure_ledger.sql").read_text()
    validate_migration_sql("009_erasure_ledger.sql", sql, runtime_role=RT)  # no raise
    assert "backfill" in sql and "entry_type" in sql  # I0 shape + I0c backfill column


def test_grant_rule_is_noop_for_single_role_selfhost():
    # No runtime role configured ⇒ single owner ⇒ grants not required.
    validate_migration_sql("006.sql", "CREATE TABLE foo (id INT);", runtime_role=None)


def test_non_create_migration_needs_no_grant():
    validate_migration_sql("007.sql", "ALTER TABLE orchestrator_agents ADD COLUMN agent_key TEXT;", runtime_role=RT)


# ── baseline verifies schema is present (must-fail: absent schema refused) ────
def test_baseline_refuses_absent_schema():
    sql = "CREATE TABLE hitl_approvers (approver_id TEXT PRIMARY KEY);"
    with pytest.raises(MigrationError):
        verify_baseline_present("005_hitl_approval.sql", sql, table_exists=lambda t: False)  # must raise


def test_baseline_accepts_present_schema():
    sql = (
        "CREATE TABLE hitl_approvers (approver_id TEXT PRIMARY KEY);\n"
        "CREATE TABLE hitl_approval_requests (request_id TEXT PRIMARY KEY);"
    )
    verify_baseline_present("005_hitl_approval.sql", sql, table_exists=lambda t: True)  # no raise


def test_baseline_refuses_when_only_some_tables_present():
    sql = (
        "CREATE TABLE hitl_approvers (approver_id TEXT);\n"
        "CREATE TABLE hitl_approval_requests (request_id TEXT);"
    )
    with pytest.raises(MigrationError):  # one present, one absent → refuse
        verify_baseline_present("005.sql", sql, table_exists=lambda t: t == "hitl_approvers")


def test_baseline_refuses_unverifiable_alter_only():
    # 007 is ALTER-only: nothing to verify → refuse (must be recorded another way).
    with pytest.raises(MigrationError):
        verify_baseline_present(
            "007.sql", "ALTER TABLE orchestrator_agents ADD COLUMN agent_key TEXT;",
            table_exists=lambda t: True,
        )


# ── boot gating: cloud never auto-migrates; self-host does; env overrides ─────
def test_cloud_disables_boot_migrations(monkeypatch):
    monkeypatch.delenv("GRAFOMEM_AUTO_MIGRATE", raising=False)
    assert boot_migrations_enabled("cloud") is False


def test_selfhost_enables_boot_migrations(monkeypatch):
    monkeypatch.delenv("GRAFOMEM_AUTO_MIGRATE", raising=False)
    assert boot_migrations_enabled("none") is True
    assert boot_migrations_enabled("token") is True


@pytest.mark.parametrize("val,expected", [("1", True), ("0", False), ("false", False), ("", False)])
def test_auto_migrate_env_overrides(monkeypatch, val, expected):
    monkeypatch.setenv("GRAFOMEM_AUTO_MIGRATE", val)
    assert boot_migrations_enabled("cloud") is expected  # override wins even in cloud


# ── bootstrap never labels pre-existing rows (applied_via nullable, no default) ─
def test_ensure_schema_migrations_applied_via_has_no_default():
    from aml.cloud.migrations_runner import _ensure_schema_migrations

    stmts = []

    class _FakeConn:
        def execute(self, sql, *a):
            stmts.append(sql)
            return self

    _ensure_schema_migrations(_FakeConn(), None)  # runtime_role None → no GRANT
    add = [s for s in stmts if "ADD COLUMN" in s and "applied_via" in s]
    assert add, "expected an ADD COLUMN applied_via statement"
    up = " ".join(add).upper()
    # A DEFAULT or NOT NULL would backfill false provenance onto pre-existing rows.
    assert "DEFAULT" not in up, "applied_via ADD COLUMN must have no DEFAULT"
    assert "NOT NULL" not in up, "applied_via ADD COLUMN must be nullable"


# ── role identifier is validated (no SQL injection via role name) ────────────
def test_check_ident_rejects_bad_role():
    with pytest.raises(MigrationError):
        _check_ident("grafomem_rt; DROP TABLE x")
    _check_ident("grafomem_rt")  # ok
    _check_ident(None)  # ok (single-role)


# ── packaging: the .sql files exist on disk AND are declared as package-data ──
def test_migration_sql_files_present_on_disk():
    files = [f.name for f in _sql_files(_migrations_dir())]
    assert "006_push_tokens.sql" in files and "007_orchestrator_agent_cgr_identity.sql" in files


def _read_migration(name: str) -> str:
    return (_migrations_dir() / name).read_text()


def test_006_satisfies_grant_rule_and_has_tenant_and_fk():
    sql = _read_migration("006_push_tokens.sql")
    # Grant present (inside the guarded DO block) ⇒ the runner accepts it.
    validate_migration_sql("006_push_tokens.sql", sql, runtime_role="grafomem_rt")
    assert "tenant_id" in sql
    assert "REFERENCES hitl_approvers" in sql and "ON DELETE CASCADE" in sql
    assert "pg_roles" in sql  # grant is guarded for single-role self-host


def test_008_convergence_is_idempotent_and_guarded():
    sql = _read_migration("008_push_tokens_converge.sql")
    assert "ADD COLUMN IF NOT EXISTS tenant_id" in sql
    assert "pg_constraint" in sql  # FK added only if absent (no ADD CONSTRAINT IF NOT EXISTS in PG)
    assert "approver_push_tokens_approver_id_fkey" in sql
    # No CREATE TABLE ⇒ grant rule not triggered; still valid.
    validate_migration_sql("008_push_tokens_converge.sql", sql, runtime_role="grafomem_rt")


def test_package_data_ships_migrations_sql():
    # Regression for the root cause: the wheel omitted cloud/migrations/*.sql, so the
    # deployed runner found zero files and applied nothing.
    import pathlib, tomllib
    root = pathlib.Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text())
    pkg_data = data["tool"]["setuptools"]["package-data"]["aml"]
    assert "cloud/migrations/*.sql" in pkg_data
