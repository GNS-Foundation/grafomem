#!/usr/bin/env python3
"""Tenant teardown — DELETE a whole tenant and its runtime-role rows.

SCOPE — read this first. This is **tenant teardown**: removing an entire tenant (its keys,
members, stores/memories, HITL enrolment, cache) and finally the `tenants` row. It is NOT, and
must not be confused with, the per-fact GDPR subject-erasure path (`aml.cloud.erasure_routes` /
`aml.cloud.erasure_sweeper`, the I0b ledger-first flow): that erases ONE data subject's fact
(`fact_ref`, GDPR Art. 17) and writes an append-only erasure certificate + ledger entry. Tenant
teardown does the opposite of appending to those ledgers — so if a tenant has ANY ledger-class row
(`cosign_dispositions`, `erasure_certificates`, `erasure_ledger`) this script REFUSES: those are
append-only custody and there is no runtime DELETE for them. This tool is for clean throwaways
(e.g. the prodverify-NNN verification tenants), not for tearing down a tenant that has accrued
signed/ledger custody.

Safety model:
- DRY-RUN by default; `--live` is required to write. Dry-run names every row it would remove,
  grouped by custody tier.
- `--live` REQUIRES an explicit tenant-id argument (positional) and prints the resolved tenant
  name for confirmation — never a fuzzy match, never "delete the tenant called X".
- PLATFORM_TENANT_IDS are protected: teardown of a platform tenant is refused — and (B7) the script
  REFUSES TO RUN AT ALL, dry-run included, when PLATFORM_TENANT_IDS is unset or empty in the local
  environment, so the guard can never be vacuous when run from an operator's machine.
- Ledger-class rows gate the whole operation: if any exist, nothing is written (the gate runs
  BEFORE any delete, so there is never a partial teardown).
- Orphan probe (B7): every OTHER tenant-scoped table (any table with a tenant_id column outside
  the walk — decision_records, audit_logs, gcrumbs_breadcrumbs, execution_receipts, tenant_deks,
  world_model_*, orchestrator_*, …) is counted per tenant. The dry-run shows the counts; `--live`
  REFUSES (exit 4, before any write) when any of them is non-zero, so a teardown never leaves
  orphaned rows behind.
- All deletes run in ONE transaction (commit once; any error rolls back).
- NEVER prints an api_key (or any secret/PII column): the plan selects only safe identifier
  columns, never `SELECT *`.

DB URL from `GRAFOMEM_ROTATE_DB_URL`, else `GRAFOMEM_MIGRATE_URL` — the migrate role, which owns
the tables (so RLS does not hide rows). NEVER the runtime role (it is SELECT-only on ledger tables
and RLS-scoped on stores).

Usage:
    python scripts/delete_tenant.py <tenant-id>            # dry-run (default)
    python scripts/delete_tenant.py <tenant-id> --live     # execute (after reviewing the dry-run)
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg.rows import dict_row

# Walk order (custody tiers). Each entry: (tier, table, tenant_column, display_columns).
# display_columns are SAFE identifiers ONLY — never a secret/PII column. tenant_column=None means
# the table is NOT tenant-scoped (global) and is reported, never deleted.
TIER_CACHE, TIER_RUNTIME, TIER_LEDGER, TIER_ROOT = "cache/ops", "runtime-role", "ledger-class", "root"

WALK: list[tuple[str, str, str | None, list[str]]] = [
    # (1) cache / ops — cheap, regenerable
    (TIER_CACHE,   "free_usage_cache",       "tenant_id", ["period_start"]),
    (TIER_CACHE,   "approver_push_tokens",   "tenant_id", ["approver_id", "platform"]),
    # (2) runtime-role rows — the tenant's operational data
    (TIER_RUNTIME, "tenant_api_keys",        "tenant_id", ["key_id", "role", "scopes"]),   # NEVER api_key
    (TIER_RUNTIME, "tenant_members",         "tenant_id", ["member_id", "role"]),           # NEVER email
    (TIER_RUNTIME, "memories",               "tenant_id", ["ref"]),                          # NEVER content*
    (TIER_RUNTIME, "memory_embeddings",      "tenant_id", ["ref"]),
    (TIER_RUNTIME, "hitl_approvers",         "tenant_id", ["approver_id", "role", "active"]),
    (TIER_RUNTIME, "hitl_approval_requests", "tenant_id", ["request_id", "status"]),
    (TIER_RUNTIME, "siem_export_cursors",    None,        []),  # GLOBAL (keyed by table_name) — reported, not deleted
    # (3) ledger-class PROBE — append-only custody; presence REFUSES the teardown (no runtime DELETE)
    (TIER_LEDGER,  "cosign_dispositions",    "tenant_id", ["record_id"]),
    (TIER_LEDGER,  "erasure_certificates",   "tenant_id", ["certificate_id"]),
    (TIER_LEDGER,  "erasure_ledger",         "tenant_id", ["entry_id"]),
    # (4) the tenant row, last
    (TIER_ROOT,    "tenants",                "id",        ["id", "name"]),
]

# Belt-and-suspenders: a display column must never be a secret/PII column.
_FORBIDDEN_DISPLAY = {"api_key", "email", "content", "content_enc", "metadata", "metadata_enc",
                      "context_bytes", "certificate", "embedding", "password_hash", "signature",
                      "public_key"}
for _tier, _tbl, _col, _disp in WALK:
    assert not (set(_disp) & _FORBIDDEN_DISPLAY), f"{_tbl}: forbidden display column"


def _db_url() -> str:
    url = os.environ.get("GRAFOMEM_ROTATE_DB_URL") or os.environ.get("GRAFOMEM_MIGRATE_URL")
    if not url:
        raise SystemExit("set GRAFOMEM_ROTATE_DB_URL (or GRAFOMEM_MIGRATE_URL) — the migrate role, not the runtime role")
    return url


def _platform_ids() -> set[str]:
    return {x.strip() for x in os.environ.get("PLATFORM_TENANT_IDS", "").split(",") if x.strip()}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
        (table,)).fetchone()
    return row is not None


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column)).fetchone()
    return row is not None


def _resolve_tenant(conn, tenant_id: str) -> dict | None:
    return conn.execute("SELECT id, name FROM tenants WHERE id = %s", (tenant_id,)).fetchone()


TIER_ORPHAN = "orphan-probe"
_WALK_TABLES = {t for _, t, _, _ in WALK}


def orphan_tables(conn) -> list[str]:
    """B7: every table in the public schema with a tenant_id column that the walk does NOT delete from.
    Discovered from information_schema, so a new tenant-scoped table is probed without a code change."""
    rows = conn.execute(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'tenant_id' ORDER BY table_name").fetchall()
    return [r["table_name"] for r in rows if r["table_name"] not in _WALK_TABLES]


def _fmt(v) -> str:
    if isinstance(v, list):
        return "[" + ",".join(str(x) for x in v) + "]"
    return str(v)


def build_plan(conn, tenant_id: str) -> list[dict]:
    """Per walk entry: existence, row count, and a sample of safe identifiers."""
    plan = []
    for tier, table, col, disp in WALK:
        entry = {"tier": tier, "table": table, "col": col, "count": 0, "rows": [],
                 "status": "ok", "note": ""}
        if not _table_exists(conn, table):
            entry["status"] = "absent"; entry["note"] = "table not present"; plan.append(entry); continue
        if col is None:
            entry["status"] = "global"; entry["note"] = "not tenant-scoped (global) — nothing to remove"
            plan.append(entry); continue
        if not _column_exists(conn, table, col):
            entry["status"] = "no-scope-col"; entry["note"] = f"no {col} column"; plan.append(entry); continue
        entry["count"] = conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE {col} = %s", (tenant_id,)).fetchone()["n"]
        if entry["count"] and disp:
            cols_sql = ", ".join(disp)
            entry["rows"] = conn.execute(
                f"SELECT {cols_sql} FROM {table} WHERE {col} = %s ORDER BY 1 LIMIT 25",
                (tenant_id,)).fetchall()
        plan.append(entry)
    # B7 orphan probe: counts only (never a sample — these tables may hold content), never deleted here.
    for table in orphan_tables(conn):
        n = conn.execute(f"SELECT count(*) AS n FROM {table} WHERE tenant_id = %s", (tenant_id,)).fetchone()["n"]
        plan.append({"tier": TIER_ORPHAN, "table": table, "col": "tenant_id", "count": n, "rows": [],
                     "status": "ok", "note": ""})
    return plan


def print_plan(plan: list[dict], tenant: dict, live: bool) -> None:
    print("\n  delete-tenant — {}".format("LIVE" if live else "DRY-RUN"))
    print(f"  tenant id:   {tenant['id']}")
    print(f"  tenant name: {tenant['name']!r}")
    print("  " + "-" * 92)
    for tier in (TIER_CACHE, TIER_RUNTIME, TIER_LEDGER, TIER_ORPHAN, TIER_ROOT):
        rows = [e for e in plan if e["tier"] == tier]
        print(f"  [{tier}]" + ("  (other tenant-scoped tables — never deleted here; any non-zero count refuses --live)"
                              if tier == TIER_ORPHAN else ""))
        for e in rows:
            head = f"    {e['table']:24} rows={e['count']:<5}"
            if e["status"] != "ok":
                print(f"{head} ({e['status']}: {e['note']})")
                continue
            print(head)
            for r in e["rows"]:
                print("        - " + ", ".join(f"{k}={_fmt(v)}" for k, v in r.items()))
            if e["count"] > len(e["rows"]):
                print(f"        … +{e['count'] - len(e['rows'])} more")
    print("  " + "-" * 92)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tenant teardown (dry-run by default).")
    ap.add_argument("tenant_id", help="EXACT tenant id to delete (no fuzzy match).")
    ap.add_argument("--live", action="store_true", help="Execute the deletes (default: dry-run).")
    args = ap.parse_args()

    platform = _platform_ids()
    if not platform:
        # B7: run from an operator's machine the variable is usually absent, which made this guard vacuous.
        print("REFUSED: PLATFORM_TENANT_IDS is not set (or empty) in this environment — export it first "
              "(the comma-separated platform tenant ids, as on the service) so the platform guard is real. "
              "Nothing was run.")
        return 2
    if args.tenant_id in platform:
        print(f"REFUSED: {args.tenant_id} is a PLATFORM_TENANT_IDS tenant — teardown is not permitted.")
        return 2

    with psycopg.connect(_db_url(), row_factory=dict_row) as conn:
        conn.autocommit = False
        tenant = _resolve_tenant(conn, args.tenant_id)
        if tenant is None:
            print(f"REFUSED: no tenant with id {args.tenant_id!r} (exact match required).")
            return 2

        plan = build_plan(conn, args.tenant_id)
        print_plan(plan, tenant, args.live)

        ledger = [e for e in plan if e["tier"] == TIER_LEDGER and e["status"] == "ok" and e["count"] > 0]
        if ledger:
            print("  REFUSED — ledger-class custody present (append-only; no runtime DELETE):")
            for e in ledger:
                print(f"      {e['table']}: {e['count']} row(s)")
            print("  This tenant cannot be torn down. Nothing was written.")
            return 3

        orphans = [e for e in plan if e["tier"] == TIER_ORPHAN and e["count"] > 0]

        deletable = [e for e in plan if e["tier"] in (TIER_CACHE, TIER_RUNTIME, TIER_ROOT)
                     and e["status"] == "ok"]
        total = sum(e["count"] for e in deletable)

        if not args.live:
            print(f"  DRY-RUN — no rows written. {total} row(s) across "
                  f"{sum(1 for e in deletable if e['count'])} table(s) would be removed.")
            if orphans:
                print("  NOTE — rows in other tenant-scoped tables would be left orphaned; --live will REFUSE (exit 4):")
                for e in orphans:
                    print(f"      {e['table']}: {e['count']} row(s)")
            print(f"  To execute: python scripts/delete_tenant.py {args.tenant_id} --live")
            conn.rollback()
            return 0

        if orphans:
            # B7: before any write. These tables are outside the walk; tearing the tenant down would leave
            # their rows with a dangling tenant_id (and signed rows — gcrumbs, receipts — unaccounted for).
            print("  REFUSED — rows in other tenant-scoped tables would be orphaned (not deleted by this tool):")
            for e in orphans:
                print(f"      {e['table']}: {e['count']} row(s)")
            print("  Nothing was written.")
            conn.rollback()
            return 4

        # LIVE: one transaction, walk order, tenants row last.
        print(f"  LIVE — tearing down tenant {tenant['id']} ({tenant['name']!r}) …")
        deleted = {}
        try:
            for e in deletable:  # WALK order is preserved in `plan`; tenants (root) is last
                if e["count"] == 0:
                    continue
                col = e["col"]
                res = conn.execute(f"DELETE FROM {e['table']} WHERE {col} = %s", (args.tenant_id,))
                deleted[e["table"]] = res.rowcount
            conn.commit()
        except Exception as exc:
            conn.rollback()
            print(f"  ERROR — rolled back, no rows deleted: {exc}")
            return 1
        for tbl, n in deleted.items():
            print(f"      deleted {n:<5} from {tbl}")
        print(f"  DONE — tenant {tenant['id']} torn down ({sum(deleted.values())} row(s)).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
