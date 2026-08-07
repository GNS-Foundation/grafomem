#!/usr/bin/env python3
"""Phase-C PRE-FLIP smoke: validate RLS enforcement AS grafomem_rt before repointing.

Run this AFTER ops/rls_decision_hitl.sql is applied to prod (creates grafomem_rt + grants +
ENABLE/FORCE RLS + policies) and BEFORE flipping GRAFOMEM_DB_URL's user to grafomem_rt. It
connects AS grafomem_rt (the restricted role the app will use) and proves, on the REAL tables,
the four properties the flip depends on — including WRITES, which a read-only smoke can't cover:

  READS   own-tenant context  ⇒ sees own rows (>0 for a tenant that has data)
          foreign-tenant ctx   ⇒ 0 rows
          unset context        ⇒ 0 rows (fail-closed)
  WRITES  own-tenant INSERT/UPDATE/DELETE succeeds (catches missing GRANT / sequence USAGE that
          surface only as permission errors under the non-owner role)

Across all three policied table families: decision_records, hitl_approval_requests, memories.

Connection: set GRAFOMEM_RT_URL to grafomem_rt's DSN (the SAME url the repoint will use).
  GRAFOMEM_RT_URL=postgresql://grafomem_rt:<pw>@host:port/db \
    python ops/rls_preflip_smoke.py --read-tenant <a-tenant-with-data>

Non-destructive: writes go to a dedicated smoke tenant and are DELETEd; it never touches a
real tenant's rows. Exit 0 = GO, non-zero = NO-GO (prints the failing property).
"""
from __future__ import annotations

import os
import sys
import uuid

TABLES = ["decision_records", "hitl_approval_requests", "memories"]


def _fail(msg):
    print(f"  ✗ NO-GO: {msg}")
    _fail.count += 1
_fail.count = 0


def _ok(msg):
    print(f"  ✓ {msg}")


def _set(conn, tenant):
    conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant or "",))


def _minimal_insert(conn, table, tenant):
    """Build a minimal INSERT for `table` from information_schema: every NOT NULL column with
    no default gets a type-appropriate dummy; tenant_id gets `tenant`. Returns the pk-ish
    filter to DELETE afterwards. Exercises the REAL write path (grants + sequence defaults)."""
    cols = conn.execute(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    names, vals = [], []
    marker = f"rls-smoke-{uuid.uuid4().hex[:8]}"
    for name, dtype, nullable, default in cols:
        if name == "tenant_id":
            names.append(name); vals.append(tenant)
        elif nullable == "NO" and default is None:
            names.append(name)
            if any(t in dtype for t in ("char", "text")):
                vals.append(marker)
            elif "bool" in dtype:
                vals.append(False)
            elif any(t in dtype for t in ("int", "numeric", "double", "real")):
                vals.append(0)
            elif "timestamp" in dtype or "date" in dtype:
                from datetime import datetime, timezone
                vals.append(datetime.now(timezone.utc))
            elif "json" in dtype:
                vals.append("{}")
            else:
                vals.append(marker)
    ph = ", ".join(["%s"] * len(names))
    casts = ", ".join(names)
    conn.execute(f"INSERT INTO {table} ({casts}) VALUES ({ph})", vals)
    return marker


def main(argv=None):
    import argparse
    import psycopg
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--read-tenant", required=True,
                    help="a real tenant_id that HAS data (own-context reads should see >0)")
    args = ap.parse_args(argv)

    url = os.environ.get("GRAFOMEM_RT_URL")
    if not url:
        sys.exit("GRAFOMEM_RT_URL not set — must be grafomem_rt's DSN (the repoint url).")
    conn = psycopg.connect(url, autocommit=True)

    print(f"[pre-flip smoke] connected as: {conn.execute('SELECT current_user').fetchone()[0]}")
    sup, byp = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
    if sup or byp:
        _fail(f"role is superuser={sup}/bypassrls={byp} — RLS would be INERT. Must be grafomem_rt.")
    else:
        _ok("role is NOSUPERUSER NOBYPASSRLS — RLS enforces")

    A = args.read_tenant
    foreign = f"nonexistent-{uuid.uuid4().hex}"
    smoke = f"rls-smoke-tenant-{uuid.uuid4().hex[:8]}"

    for t in TABLES:
        print(f"[{t}]")
        # READS -------------------------------------------------------------
        _set(conn, A)
        own = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        (_ok if own > 0 else _fail)(f"own-tenant read sees {own} rows (want >0 for a tenant with data)")
        _set(conn, foreign)
        fc = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        (_ok if fc == 0 else _fail)(f"foreign-tenant read sees {fc} rows (want 0)")
        _set(conn, None)
        uc = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        (_ok if uc == 0 else _fail)(f"unset-context read sees {uc} rows (want 0 — fail-closed)")
        # WRITES (own-tenant, non-destructive: smoke tenant, cleaned up) ----
        try:
            _set(conn, smoke)
            marker = _minimal_insert(conn, t, smoke)
            conn.execute(f"UPDATE {t} SET tenant_id=%s WHERE tenant_id=%s", (smoke, smoke))
            seen = conn.execute(f"SELECT count(*) FROM {t} WHERE tenant_id=%s", (smoke,)).fetchone()[0]
            conn.execute(f"DELETE FROM {t} WHERE tenant_id=%s", (smoke,))
            (_ok if seen >= 1 else _fail)(f"own-tenant INSERT/UPDATE/DELETE succeeded (grants + sequence USAGE OK)")
        except Exception as e:
            _fail(f"own-tenant WRITE failed — likely missing GRANT/sequence USAGE: {type(e).__name__}: {str(e)[:120]}")
    conn.close()

    print()
    if _fail.count == 0:
        print("RESULT: GO — enforcement holds, reads scoped, own-tenant writes succeed on all 3 families.")
        sys.exit(0)
    print(f"RESULT: NO-GO — {_fail.count} check(s) failed. Do NOT repoint.")
    sys.exit(1)


if __name__ == "__main__":
    main()
