#!/usr/bin/env python3
"""CLI for the hash-at-rest backfill (also runs in-process from the migrations runner).

The core lives in aml.cloud.api_key_hash_backfill; this is a thin wrapper for manual/operator use.

- FAIL CLOSED (exit 3) on an absent/empty GRAFOMEM_API_KEY_PEPPER — hashing under an empty pepper is
  a silent catastrophe. `--skip-if-no-pepper` instead stays dark (exit 0), for a pepper-less env.
- The pepper never enters SQL (runner-side HMAC). gfm_ key values are never printed.

DB URL: GRAFOMEM_ROTATE_DB_URL, else GRAFOMEM_MIGRATE_URL — the migrate role, never the runtime role.
"""
from __future__ import annotations

import os
import sys

# Re-export so existing callers/tests can use scripts' `backfill`/`run` directly.
from aml.cloud.api_key_hash_backfill import backfill, run  # noqa: F401
from aml.server.api_key_hash import PepperMissing, get_pepper


def _db_url() -> str:
    url = os.environ.get("GRAFOMEM_ROTATE_DB_URL") or os.environ.get("GRAFOMEM_MIGRATE_URL")
    if not url:
        raise SystemExit("set GRAFOMEM_ROTATE_DB_URL (or GRAFOMEM_MIGRATE_URL) — the migrate role")
    return url


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    skip = "--skip-if-no-pepper" in argv
    # Check the pepper BEFORE the DB URL so an absent pepper refuses/skips without needing a DB.
    try:
        get_pepper()
    except PepperMissing as e:
        if skip:
            print("backfill: skipped — GRAFOMEM_API_KEY_PEPPER not set (dark rollout, no hashes written)")
            return 0
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    res = run(_db_url(), skip_if_no_pepper=skip)
    print(f"backfill: scanned={res['scanned']} updated={res['updated']} (api_key_hash populated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
