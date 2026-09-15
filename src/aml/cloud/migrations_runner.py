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
    """Tables `sql` GRANTs some privilege on to `role` (best-effort SQL scan)."""
    pat = re.compile(
        r"grant\s+[^;]*?\bon\s+(?:table\s+)?[\"']?(?:public\.)?([a-z_][a-z0-9_]*)"
        r"[^;]*?\bto\s+\"?" + re.escape(role) + r"\b",
        re.IGNORECASE | re.DOTALL,
    )
    return {m.group(1).lower() for m in pat.finditer(sql)}


def validate_migration_sql(
    name: str, sql: str, runtime_role: str | None, ledger_role: str = LEDGER_ROLE
) -> None:
    """Enforce the grant rule. No-op when `runtime_role` is None (single-role)."""
    if not runtime_role:
        return
    created = {m.group(1).lower() for m in _CREATE_TABLE_RE.finditer(sql)}
    if not created:
        return
    missing = created - _granted_tables(sql, runtime_role)
    if missing:
        raise MigrationError(
            f"{name}: CREATE TABLE {sorted(missing)} without a GRANT to {runtime_role!r} "
            f"in the same migration. The runtime role owns no tables in a split-role "
            f"deployment; add e.g. "
            f"`GRANT SELECT, INSERT, UPDATE, DELETE ON <table> TO {runtime_role};`"
        )
    ledger_created = {t for t in created if "ledger" in t}
    missing_ledger = ledger_created - _granted_tables(sql, ledger_role)
    if missing_ledger:
        raise MigrationError(
            f"{name}: ledger table {sorted(missing_ledger)} without a GRANT to "
            f"{ledger_role!r}. Ledger tables are written by the ledger role too."
        )


def _check_ident(role: str | None) -> None:
    if role is not None and not _IDENT_RE.match(role):
        raise MigrationError(f"invalid role identifier: {role!r}")


# ── schema_migrations ledger ─────────────────────────────────────────────────

def _ensure_schema_migrations(conn: "psycopg.Connection", runtime_role: str | None) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version     TEXT PRIMARY KEY,
               applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
               applied_via TEXT NOT NULL DEFAULT 'runner'
           )"""
    )
    # For pre-existing ledgers created before this column existed.
    conn.execute(
        "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS applied_via TEXT NOT NULL DEFAULT 'runner'"
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
    ``applied_via='baseline'`` so provenance is explicit. Verify the schema is
    actually present first (operator step) — this only writes the ledger row.
    """
    if runtime_role is None:
        runtime_role = os.environ.get(RUNTIME_ROLE_ENV) or None
    _check_ident(runtime_role)
    known = {f.name for f in _sql_files(migrations_dir or _migrations_dir())}
    unknown = [v for v in versions if v not in known]
    if unknown:
        raise MigrationError(f"unknown migrations to baseline (not in migrations dir): {unknown}")
    with _connect(migrate_url) as conn:
        conn.autocommit = False
        _ensure_schema_migrations(conn, runtime_role)
        recorded = []
        for version in versions:
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_via) VALUES (%s, 'baseline') "
                "ON CONFLICT (version) DO NOTHING",
                (version,),
            )
            recorded.append(version)
        conn.commit()
    logger.info("baselined migrations (no SQL run): %s", recorded)
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
    else:
        print(apply_migrations(args.url))


if __name__ == "__main__":
    main()
