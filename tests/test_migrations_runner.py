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


# ── grant scan ignores comments (validator false-match fix) ──────────────────
def test_grant_only_in_comment_is_rejected():
    """MUST-FAIL: a migration whose ONLY grant-shape is inside a comment has no real GRANT,
    so it must be REJECTED. Before the strip-comments fix the scanner false-matched the
    comment prose as a real `GRANT ... ON ... TO ...` and wrongly PASSED."""
    sql = (
        "CREATE TABLE foo (id INT);\n"
        "-- GRANT SELECT, INSERT, UPDATE, DELETE ON foo TO grafomem_rt;\n"  # comment only
    )
    with pytest.raises(MigrationError):
        validate_migration_sql("x.sql", sql, runtime_role=RT)


def test_grant_shaped_prose_in_comment_does_not_satisfy_rule():
    """The exact prose that first tripped the bug: a comment containing 'grant … on … to'
    must not count as granting the runtime role."""
    sql = (
        "CREATE TABLE foo (id INT);\n"
        "-- the grant only ever runs on a fresh split-role install, applied to grafomem_rt\n"
    )
    with pytest.raises(MigrationError):
        validate_migration_sql("x.sql", sql, runtime_role=RT)


def test_real_grant_still_detected_positive_control():
    """POSITIVE CONTROL: a real (non-comment) GRANT is still detected and passes — including
    when a grant-shaped comment sits right next to it."""
    sql = (
        "CREATE TABLE foo (id INT);\n"
        "-- grant note: on apply this is granted to grafomem_rt\n"
        "GRANT SELECT, INSERT, UPDATE, DELETE ON foo TO grafomem_rt;\n"
    )
    validate_migration_sql("x.sql", sql, runtime_role=RT)  # no raise


def test_real_grant_inside_guarded_do_block_passes():
    """The guarded DO-block form used by 004/005/006 survives comment stripping (the GRANT
    is inside $$…$$, not a comment)."""
    sql = (
        "CREATE TABLE foo (id INT);\n"
        "-- Runtime role receives DML; guarded no-op in single-role self-host.\n"
        "DO $$\nBEGIN\n"
        "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN\n"
        "    GRANT SELECT, INSERT, UPDATE, DELETE ON foo TO grafomem_rt;\n"
        "  END IF;\nEND $$;\n"
    )
    validate_migration_sql("x.sql", sql, runtime_role=RT)  # no raise


# ── ledger-class rule (declared by `-- class: ledger` header marker) ─────────
_LEDGER_OK = (
    "-- class: ledger\n"
    "CREATE TABLE erasure_ledger (entry_id TEXT PRIMARY KEY);\n"
    "GRANT SELECT, INSERT ON erasure_ledger TO grafomem_ledger;\n"
    "GRANT SELECT ON erasure_ledger TO grafomem_rt;\n"
    "REVOKE INSERT, UPDATE, DELETE ON erasure_ledger FROM grafomem_rt;\n"
)


def test_ledger_class_compliant_passes():
    validate_migration_sql("010_x_ledger.sql", _LEDGER_OK, runtime_role=RT)  # no raise


def test_ledger_class_rt_write_grant_rejected():  # must-fail (a)
    bad = _LEDGER_OK + "GRANT INSERT ON erasure_ledger TO grafomem_rt;\n"
    with pytest.raises(MigrationError):
        validate_migration_sql("010_x_ledger.sql", bad, runtime_role=RT)


def test_ledger_class_missing_revoke_rejected():  # must-fail (b)
    bad = _LEDGER_OK.replace("REVOKE INSERT, UPDATE, DELETE ON erasure_ledger FROM grafomem_rt;\n", "")
    with pytest.raises(MigrationError):
        validate_migration_sql("010_x_ledger.sql", bad, runtime_role=RT)


def test_ledger_class_ledger_missing_insert_rejected():  # must-fail (c)
    bad = _LEDGER_OK.replace(
        "GRANT SELECT, INSERT ON erasure_ledger TO grafomem_ledger;\n",
        "GRANT SELECT ON erasure_ledger TO grafomem_ledger;\n",
    )
    with pytest.raises(MigrationError):
        validate_migration_sql("010_x_ledger.sql", bad, runtime_role=RT)


def test_ledger_class_ledger_write_grant_rejected():  # must-fail (d) — append-only
    bad = _LEDGER_OK + "GRANT UPDATE ON erasure_ledger TO grafomem_ledger;\n"
    with pytest.raises(MigrationError):
        validate_migration_sql("010_x_ledger.sql", bad, runtime_role=RT)


def test_ledger_class_is_by_marker_not_name():
    # A table named *ledger* WITHOUT the marker is a plain table: only the rt grant is required.
    sql = (
        "CREATE TABLE erasure_ledger (entry_id TEXT PRIMARY KEY);\n"
        "GRANT SELECT ON erasure_ledger TO grafomem_rt;\n"
    )
    validate_migration_sql("00x_no_marker.sql", sql, runtime_role=RT)  # no raise (no marker ⇒ plain rule)


def test_real_010_revoke_migration_valid():
    sql = (_migrations_dir() / "010_erasure_ledger_revoke.sql").read_text()
    assert sql.lstrip().lower().startswith("-- class: ledger")
    assert "REVOKE INSERT, UPDATE, DELETE ON erasure_ledger FROM grafomem_rt" in sql
    # No CREATE TABLE ⇒ the per-created-table ledger matrix does not apply; validates fine.
    validate_migration_sql("010_erasure_ledger_revoke.sql", sql, runtime_role=RT)  # no raise


def test_real_009_is_marked_and_grandfathered():
    sql = (_migrations_dir() / "009_erasure_ledger.sql").read_text()
    assert sql.lstrip().lower().startswith("-- class: ledger")  # header marker present
    assert "backfill" in sql and "entry_type" in sql  # I0 shape + I0c backfill column
    # 009 predates the REVOKE rule and is applied everywhere we control → grandfathered, so it
    # validates despite carrying the marker without an explicit rt REVOKE.
    validate_migration_sql("009_erasure_ledger.sql", sql, runtime_role=RT)  # no raise (grandfathered)


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
