#!/usr/bin/env python3
"""Runner-side backfill: tenant_api_keys.api_key_hash = HMAC_SHA256(pepper, api_key).

Part of hash-at-rest PR 2 (DARK — auth still resolves by plaintext). Run in the pre-deploy AFTER
migration 017, as the migrate role.

Two guarantees this script exists to keep:
- **Fail closed on the pepper.** An absent/empty GRAFOMEM_API_KEY_PEPPER refuses (exit 3, no writes).
  Hashing under an empty pepper is a silent catastrophe (every hash wrong, discovered at first auth).
- **The pepper never enters SQL.** Hashes are computed in THIS process; only the resulting bytes and
  a key_id go to the DB, via a parameterized UPDATE. So the pepper cannot surface in
  pg_stat_statements, query logs, or error text.

Idempotent: only rows with api_key_hash IS NULL are touched. gfm_ key values are never printed.

DB URL: GRAFOMEM_ROTATE_DB_URL, else GRAFOMEM_MIGRATE_URL — the migrate role, never the runtime role.
"""
from __future__ import annotations

import os
import sys

import psycopg
from psycopg.rows import dict_row

from aml.server.api_key_hash import PepperMissing, compute_api_key_hash, get_pepper


def _db_url() -> str:
    url = os.environ.get("GRAFOMEM_ROTATE_DB_URL") or os.environ.get("GRAFOMEM_MIGRATE_URL")
    if not url:
        raise SystemExit("set GRAFOMEM_ROTATE_DB_URL (or GRAFOMEM_MIGRATE_URL) — the migrate role")
    return url


def backfill(conn, pepper: str) -> dict:
    """Populate api_key_hash for every row missing it. `conn` is a psycopg connection (dict rows).

    The pepper is used ONLY as HMAC input in-process; the UPDATE is parameterized on the hash bytes
    and key_id — the pepper is never part of any SQL string sent to the server.
    """
    rows = conn.execute(
        "SELECT key_id, api_key FROM tenant_api_keys "
        "WHERE api_key_hash IS NULL AND api_key IS NOT NULL"
    ).fetchall()
    updated = 0
    for r in rows:
        h = compute_api_key_hash(r["api_key"], pepper)
        conn.execute(
            "UPDATE tenant_api_keys SET api_key_hash = %s WHERE key_id = %s",
            (h, r["key_id"]),
        )
        updated += 1
    return {"scanned": len(rows), "updated": updated}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Pre-deploy gate: --skip-if-no-pepper lets a pepper-less environment stay DARK (skip, exit 0)
    # instead of failing the deploy. Direct invocation (no flag) stays FAIL CLOSED — an intended
    # backfill with no pepper is an error (exit 3), never a silent hash under an empty pepper.
    skip_if_absent = "--skip-if-no-pepper" in argv
    try:
        pepper = get_pepper()  # FAIL CLOSED before touching the DB
    except PepperMissing as e:
        if skip_if_absent:
            print("backfill: skipped — GRAFOMEM_API_KEY_PEPPER not set (dark rollout, no hashes written)")
            return 0
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    with psycopg.connect(_db_url(), row_factory=dict_row, autocommit=False) as conn:
        try:
            res = backfill(conn, pepper)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    print(f"backfill: scanned={res['scanned']} updated={res['updated']} (api_key_hash populated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
