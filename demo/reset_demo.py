"""Task 5 — wipe the demo tenant's DATA (decisions, receipts, memories, …) while
leaving the tenant itself (identity + API key) intact, so rehearsals start clean.

Deletes every public table row scoped to the demo tenant_id, except the tenant
identity tables. DB URL comes from GRAFOMEM_DB_URL (env only — never hardcoded).
"""
from __future__ import annotations

import os
import sys

import psycopg

from common import load_creds

PRESERVE = {"tenants", "tenant_api_keys", "tenant_deks", "tenant_members"}


def main() -> None:
    db_url = os.environ.get("GRAFOMEM_DB_URL")
    if not db_url:
        sys.exit("blocked — no GRAFOMEM_DB_URL")
    tenant_id = load_creds()["tenant_id"]
    print(f"Resetting data for tenant {tenant_id}")

    with psycopg.connect(db_url, autocommit=True) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema='public' AND column_name='tenant_id' "
            "ORDER BY table_name"
        ).fetchall()]
        targets = [t for t in tables if t not in PRESERVE]

        def total() -> int:
            n = 0
            for t in targets:
                n += conn.execute(f"SELECT count(*) FROM {t} WHERE tenant_id = %s", (tenant_id,)).fetchone()[0]
            return n

        before = total()
        print(f"  demo-tenant rows BEFORE: {before}  (across {len(targets)} tables)")
        for t in targets:
            c = conn.execute(f"DELETE FROM {t} WHERE tenant_id = %s", (tenant_id,)).rowcount
            if c:
                print(f"    deleted {c:>4} from {t}")
        after = total()
        print(f"  demo-tenant rows AFTER:  {after}")

    # Confirm the tenant + key still exist.
    with psycopg.connect(db_url, autocommit=True) as conn:
        still = conn.execute("SELECT count(*) FROM tenants WHERE id = %s", (tenant_id,)).fetchone()[0]
    print(f"  tenant still present: {'yes' if still else 'NO'}  (api key preserved)")
    print("OK" if after == 0 and still else "WARNING: unexpected state")


if __name__ == "__main__":
    main()
