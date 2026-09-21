"""Schema migrations for grafomem.

DDL is a **deliberate release step** run by a privileged migrate role — never DDL
from the runtime process. Two facts forced this:

  * In production every table is owned by ``postgres``; the app runs as
    ``grafomem_rt``, which owns no tables and cannot run DDL. A boot-time runner
    using the runtime connection can never apply a migration.
  * The migration ``.sql`` files must be shipped in the wheel (see
    ``[tool.setuptools.package-data]``) — deployed containers run the installed
    wheel, not the source tree. When they were absent, the boot runner found zero
    files and logged "done" having applied nothing.

Usage (release step, as the migrate role)::

    GRAFOMEM_MIGRATE_URL=postgresql://grafomem_migrate:...@host/db \
    GRAFOMEM_RUNTIME_ROLE=grafomem_rt \
        python -m aml.cloud.migrations_runner                 # apply pending
    ... python -m aml.cloud.migrations_runner --baseline 001_x.sql,002_y.sql   # record, no re-run

**Grant rule.** In a split-role deployment (``GRAFOMEM_RUNTIME_ROLE`` set) every
migration that ``CREATE``s a table MUST, in the same file, ``GRANT`` the runtime
role the DML it needs — and ``grafomem_ledger`` for ledger tables — or the runtime
process (which owns no tables) cannot use the new table. The runner refuses such a
migration. In single-role self-host (no runtime role) the rule is a no-op.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path

import psycopg

logger = logging.getLogger("grafomem.migrations")

RUNTIME_ROLE_ENV = "GRAFOMEM_RUNTIME_ROLE"  # e.g. "grafomem_rt"; unset ⇒ single-role self-host
LEDGER_ROLE = "grafomem_ledger"
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class MigrationError(Exception):
    """A migration is malformed or violates the grant rule."""


# ── discovery ────────────────────────────────────────────────────────────────

def _migrations_dir() -> Path:
    return Path(__file__).parent / "migrations"


def _sql_files(migrations_dir: Path) -> list[Path]:
    if not migrations_dir.exists():
        return []
    return sorted(f for f in migrations_dir.iterdir() if f.name.endswith(".sql"))


# ── grant-rule validation ────────────────────────────────────────────────────

_CREATE_TABLE_RE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"']?(?:public\.)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


def _granted_tables(sql: str, role: str) -> set[str]:
    """Tables `sql` GRANTs some privilege on to `role` (best-effort SQL scan).

    Accepts a **multi-role TO clause** (``... TO a, b``) and grants wrapped in a
    ``DO $$ … $$`` guard: the role list after ``TO`` is captured up to the statement's
    ``;`` and `role` is matched as a whole word anywhere in it.
    """
    granted: set[str] = set()
    for m in re.finditer(
        r"grant\s+[^;]*?\bon\s+(?:table\s+)?[\"']?(?:public\.)?([a-z_][a-z0-9_]*)\b"
        r"[^;]*?\bto\b([^;]*)",
        sql,
        re.IGNORECASE | re.DOTALL,
    ):
        table, roles_clause = m.group(1), m.group(2)
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(role) + r"(?![A-Za-z0-9_])", roles_clause):
            granted.add(table.lower())
    return granted


# A migration declares itself ledger-class with a header marker — NOT inferred from
# the table name. Ledger-class tables are append-only: the ledger role writes+reads,
# the runtime role is read-only.
_LEDGER_CLASS_MARKER = re.compile(r"^\s*--\s*class:\s*ledger\b", re.IGNORECASE | re.MULTILINE)
_WRITE_PRIVS = frozenset({"INSERT", "UPDATE", "DELETE"})

# Migrations that predate the ledger-class rule and are already applied in every
# environment we control (so the runner never re-validates them here). Kept
# header-marked for classification, but exempt from the REVOKE requirement — a fresh
# split-role install is the only residual (see the design report). New ledger-class
# migrations get no such exemption.
_LEDGER_RULE_GRANDFATHERED = frozenset({"009_erasure_ledger.sql"})


def _stmt_privs(sql: str, verb: str, connector: str, table: str, role: str) -> set[str]:
    """Privileges named in `verb` (GRANT/REVOKE) statements on `table` to/from `role`.

    `connector` is 'to' for GRANT, 'from' for REVOKE. Handles multi-role clauses and
    grants/revokes wrapped in DO-blocks (scans to the statement's ';'). `ALL` expands.
    """
    privs: set[str] = set()
    pat = re.compile(
        verb + r"\s+(.+?)\s+on\s+(?:table\s+)?[\"']?(?:public\.)?" + re.escape(table)
        + r"\b[^;]*?\b" + connector + r"\b([^;]*)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pat.finditer(sql):
        priv_str, roles_clause = m.group(1).upper(), m.group(2)
        if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(role) + r"(?![A-Za-z0-9_])", roles_clause):
            continue
        if re.search(r"\bALL\b", priv_str):
            privs |= {"SELECT", *_WRITE_PRIVS}
        for p in ("SELECT", *_WRITE_PRIVS):
            if re.search(r"\b" + p + r"\b", priv_str):
                privs.add(p)
    return privs


def _strip_sql_comments(sql: str) -> str:
    """Return `sql` with `--` line and `/* */` block comments removed (PostgreSQL block
    comments nest), preserving single-quoted string literals and `$tag$` dollar-quoted
    bodies so real statements inside a DO-block are untouched.

    The grant/revoke/CREATE scans run on the stripped text: grant-shaped PROSE inside a
    comment (e.g. "the grant only ever runs on a fresh install") must NOT be read as a real
    `GRANT ... ON ... TO ...`. The `-- class: ledger` marker is a deliberate comment and is
    matched on the RAW sql by the caller, before this runs.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":  # single-quoted string literal ('' escapes a quote)
            out.append(c); i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append(sql[i + 1]); i += 2; continue
                    i += 1; break
                i += 1
            continue
        if c == "$":  # dollar-quoted string: $tag$ ... $tag$
            m = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                if end == -1:
                    out.append(sql[i:]); i = n
                else:
                    out.append(sql[i:end + len(tag)]); i = end + len(tag)
                continue
        if sql[i:i + 2] == "--":  # line comment → drop to end of line, keep the newline
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if sql[i:i + 2] == "/*":  # block comment (nestable) → replace with a space
            depth, i = 1, i + 2
            while i < n and depth:
                if sql[i:i + 2] == "/*":
                    depth += 1; i += 2
                elif sql[i:i + 2] == "*/":
                    depth -= 1; i += 2
                else:
                    i += 1
            out.append(" ")
            continue
        out.append(c); i += 1
    return "".join(out)


def validate_migration_sql(
    name: str, sql: str, runtime_role: str | None, ledger_role: str = LEDGER_ROLE
) -> None:
    """Enforce the grant rule. No-op when `runtime_role` is None (single-role)."""
    if not runtime_role:
        return
    # The `-- class: ledger` marker is a deliberate comment — read it from the RAW sql.
    is_ledger_class = bool(_LEDGER_CLASS_MARKER.search(sql))
    # Everything else scans the COMMENT-STRIPPED sql, so grant-shaped prose in a comment
    # cannot masquerade as a real GRANT/REVOKE (and a commented-out CREATE TABLE is ignored).
    scan_sql = _strip_sql_comments(sql)
    created = {m.group(1).lower() for m in _CREATE_TABLE_RE.finditer(scan_sql)}
    if not created:
        return
    missing = created - _granted_tables(scan_sql, runtime_role)
    if missing:
        raise MigrationError(
            f"{name}: CREATE TABLE {sorted(missing)} without a GRANT to {runtime_role!r} "
            f"in the same migration. The runtime role owns no tables in a split-role "
            f"deployment; add e.g. "
            f"`GRANT SELECT, INSERT, UPDATE, DELETE ON <table> TO {runtime_role};`"
        )
    # Ledger-class rule (declared by the `-- class: ledger` header marker, not the name).
    # Append-only: ledger role INSERT+SELECT (it also reads for restore-scrub), no
    # UPDATE/DELETE; runtime role SELECT-only, which requires an explicit REVOKE because
    # ALTER DEFAULT PRIVILEGES grants the runtime role full DML on every migrate-created table.
    if is_ledger_class and name not in _LEDGER_RULE_GRANDFATHERED:
        for tbl in sorted(created):
            rt_granted = _stmt_privs(scan_sql, "grant", "to", tbl, runtime_role)
            rt_revoked = _stmt_privs(scan_sql, "revoke", "from", tbl, runtime_role)
            led_granted = _stmt_privs(scan_sql, "grant", "to", tbl, ledger_role)
            if rt_granted & _WRITE_PRIVS:
                raise MigrationError(
                    f"{name}: ledger-class table {tbl!r} grants "
                    f"{sorted(rt_granted & _WRITE_PRIVS)} to {runtime_role!r}; it must be SELECT-only."
                )
            if not _WRITE_PRIVS <= rt_revoked:
                raise MigrationError(
                    f"{name}: ledger-class table {tbl!r} must "
                    f"`REVOKE INSERT, UPDATE, DELETE ON {tbl} FROM {runtime_role};` — ALTER DEFAULT "
                    f"PRIVILEGES grants the runtime role full DML on migrate-created tables."
                )
            if not {"INSERT", "SELECT"} <= led_granted:
                raise MigrationError(
                    f"{name}: ledger-class table {tbl!r} must GRANT INSERT + SELECT to "
                    f"{ledger_role!r} (it appends and reads back for restore-scrub)."
                )
            if led_granted & {"UPDATE", "DELETE"}:
                raise MigrationError(
                    f"{name}: ledger-class table {tbl!r} grants "
                    f"{sorted(led_granted & {'UPDATE', 'DELETE'})} to {ledger_role!r}; a ledger is append-only."
                )


def _check_ident(role: str | None) -> None:
    if role is not None and not _IDENT_RE.match(role):
        raise MigrationError(f"invalid role identifier: {role!r}")


# ── baseline verification ────────────────────────────────────────────────────

def _created_tables(sql: str) -> set[str]:
    return {m.group(1).lower() for m in _CREATE_TABLE_RE.finditer(sql)}


def verify_baseline_present(version: str, sql: str, table_exists) -> None:
    """Refuse to baseline a version whose schema is not actually present.

    Baseline records an *already-applied* migration without running it, so it must
    only be used when the objects genuinely exist. `table_exists(name) -> bool` is
    the check. A version with no `CREATE TABLE` we can verify (e.g. an ALTER-only
    migration) is refused — baseline is only for schema we can confirm present.
    """
    created = _created_tables(sql)
    if not created:
        raise MigrationError(
            f"{version}: cannot baseline — no CREATE TABLE to verify. Baseline is only "
            f"for migrations whose objects can be confirmed present; record this another way."
        )
    missing = sorted(t for t in created if not table_exists(t))
    if missing:
        raise MigrationError(
            f"{version}: refusing to baseline — schema not present: {missing}. "
            f"Baseline is for an already-applied migration; apply it via the runner instead."
        )


def _table_exists(conn: "psycopg.Connection", name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (f"public.{name}",)).fetchone()
    return bool(row and row[0] is not None)


# ── schema_migrations ledger ─────────────────────────────────────────────────

def _ensure_schema_migrations(conn: "psycopg.Connection", runtime_role: str | None) -> None:
    # `applied_via` is NULLABLE with NO default. A row that pre-existed this column —
    # recorded by an older runner, or a migration applied out-of-band (e.g. by hand as
    # `postgres`) — reads NULL, i.e. "provenance unknown". Every row this runner writes
    # sets it explicitly ('runner' on apply, 'baseline' on baseline), so bootstrap NEVER
    # labels a row it did not itself write. A DEFAULT here would backdate false provenance
    # onto pre-existing rows.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version     TEXT PRIMARY KEY,
               applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
               applied_via TEXT
           )"""
    )
    conn.execute(
        "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS applied_via TEXT"
    )
    if runtime_role:
        conn.execute(f"GRANT SELECT ON schema_migrations TO {runtime_role}")


# ── apply / baseline ─────────────────────────────────────────────────────────

def _connect(url: str) -> "psycopg.Connection":
    return psycopg.connect(
        url,
        connect_timeout=int(os.environ.get("GRAFOMEM_DB_CONNECT_TIMEOUT", "5")),
    )


def apply_migrations(
    migrate_url: str, *, migrations_dir: Path | None = None, runtime_role: str | None = None
) -> dict:
    """Apply every pending migration, each in its own transaction with its ledger row.

    Run as the migrate role. Validates the grant rule before executing each file.
    """
    if runtime_role is None:
        runtime_role = os.environ.get(RUNTIME_ROLE_ENV) or None
    _check_ident(runtime_role)
    migrations_dir = migrations_dir or _migrations_dir()
    files = _sql_files(migrations_dir)
    result: dict = {"applied": [], "skipped": [], "files": len(files)}
    if not files:
        logger.warning(
            "no migration .sql files in %s — is package-data shipping cloud/migrations/*.sql?",
            migrations_dir,
        )
        return result
    with _connect(migrate_url) as conn:
        conn.autocommit = False
        _ensure_schema_migrations(conn, runtime_role)
        conn.commit()
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for f in files:
            version = f.name
            if version in applied:
                result["skipped"].append(version)
                continue
            sql = f.read_text()
            validate_migration_sql(version, sql, runtime_role)  # raises before any write
            try:
                with conn.transaction():  # DDL + ledger row atomically
                    conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_via) VALUES (%s, 'runner')",
                        (version,),
                    )
                logger.info("applied migration %s", version)
                result["applied"].append(version)
            except Exception as e:
                logger.error("failed to apply %s: %s", version, e)
                raise
    return result


def baseline_migrations(
    migrate_url: str, versions: list[str], *, migrations_dir: Path | None = None,
    runtime_role: str | None = None,
) -> dict:
    """Record `versions` as applied **without running their SQL**.

    For migrations already applied out-of-band (e.g. by hand as ``postgres``): the
    objects exist, so re-running would fail or duplicate. Recorded with
    ``applied_via='baseline'`` so provenance is explicit. Each version's schema is
    **verified present** (its CREATE TABLE targets must exist) before it is recorded
    — baseline of an absent schema is refused.
    """
    if runtime_role is None:
        runtime_role = os.environ.get(RUNTIME_ROLE_ENV) or None
    _check_ident(runtime_role)
    files = {f.name: f for f in _sql_files(migrations_dir or _migrations_dir())}
    unknown = [v for v in versions if v not in files]
    if unknown:
        raise MigrationError(f"unknown migrations to baseline (not in migrations dir): {unknown}")
    with _connect(migrate_url) as conn:
        conn.autocommit = False
        _ensure_schema_migrations(conn, runtime_role)
        conn.commit()
        recorded = []
        for version in versions:
            sql = files[version].read_text()
            verify_baseline_present(version, sql, lambda t: _table_exists(conn, t))  # refuses if absent
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_via) VALUES (%s, 'baseline') "
                "ON CONFLICT (version) DO NOTHING",
                (version,),
            )
            recorded.append(version)
        conn.commit()
    logger.info("baselined migrations (verified present, no SQL run): %s", recorded)
    return {"baselined": recorded}


# ── boot integration ─────────────────────────────────────────────────────────

def boot_migrations_enabled(auth_mode: str) -> bool:
    """Whether the app should apply migrations at boot.

    Self-host: ON by default (a solo operator wants the app to migrate itself).
    Cloud: OFF — a runtime process must never attempt DDL; migrations are a release
    step. `GRAFOMEM_AUTO_MIGRATE` overrides either way.
    """
    override = os.environ.get("GRAFOMEM_AUTO_MIGRATE")
    if override is not None:
        return override.strip().lower() not in ("", "0", "false", "no")
    return auth_mode != "cloud"


def boot_apply_if_enabled(db_url: str, auth_mode: str) -> None:
    """Boot entrypoint: apply in self-host, no-op (and say so once) in cloud."""
    if not boot_migrations_enabled(auth_mode):
        logger.info(
            "boot migrations disabled (auth_mode=%s) — DDL is a release step "
            "(python -m aml.cloud.migrations_runner as the migrate role); skipping",
            auth_mode,
        )
        return
    apply_migrations(db_url)


# ── CLI (the release step) ───────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Apply grafomem schema migrations (release step).")
    ap.add_argument(
        "--baseline",
        help="comma-separated migration filenames to RECORD as applied without running "
        "(for migrations already applied out-of-band).",
    )
    ap.add_argument(
        "--url",
        default=os.environ.get("GRAFOMEM_MIGRATE_URL") or os.environ.get("GRAFOMEM_DB_URL"),
        help="DB URL for the migrate role (default: $GRAFOMEM_MIGRATE_URL, then $GRAFOMEM_DB_URL).",
    )
    ap.add_argument(
        "--ensure-schema",
        action="store_true",
        help="A1 release step: run every service's ensure_schema as the migrate role "
        "(create the ensure_schema-owned base tables), THEN apply pending migrations. "
        "This is the pre-deploy step; cloud boot itself does no DDL.",
    )
    args = ap.parse_args(argv)
    if not args.url:
        raise SystemExit("set GRAFOMEM_MIGRATE_URL (the grafomem_migrate role) to run migrations")
    if not os.environ.get("GRAFOMEM_MIGRATE_URL"):
        logger.warning(
            "GRAFOMEM_MIGRATE_URL is unset; falling back to GRAFOMEM_DB_URL — the runtime "
            "role usually cannot run DDL, so this will likely fail. Configure a migrate role."
        )
    if args.baseline:
        versions = [v.strip() for v in args.baseline.split(",") if v.strip()]
        print(baseline_migrations(args.url, versions))
        return
    if args.ensure_schema:
        # Build the app with ensure_schema forced on, pointed at the migrate role, which
        # runs every service's ensure_schema synchronously and raises on any failure
        # (so this command exits non-zero and blocks the deploy). Reuses create_app's
        # exact service construction — no separate service list to drift.
        from aml.server.app import create_app
        logger.info("ensure-schema: building services + running ensure_schema as the migrate role")
        create_app(db_url=args.url, ensure_schema_only=True)
        logger.info("ensure-schema: done; applying pending migrations")
    print(apply_migrations(args.url))
    # Hash-at-rest (DARK): backfill tenant_api_keys.api_key_hash IN THIS PROCESS — the pre-deploy
    # runs a single command, so this must not depend on shell chaining. Skip when the pepper is
    # unset (dark rollout, auth still plaintext); where it is set (staging/prod), populate hashes.
    from aml.cloud.api_key_hash_backfill import run as _backfill_api_key_hash
    _backfill_api_key_hash(args.url, skip_if_no_pepper=True)


if __name__ == "__main__":
    main()
