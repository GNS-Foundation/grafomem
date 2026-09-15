#!/usr/bin/env python3
"""Precautionary rotate-all-tenant-keys. DRY-RUN by default; --live to rotate.

Rotates every tenant's API key(s) the same way the rotate-key route does
(`DELETE FROM tenant_api_keys WHERE tenant_id=…` then mint one `default_admin`
key with TENANT_ADMIN_SCOPES), EXCEPT:

  * **platform tenants** (`PLATFORM_TENANT_IDS`) — the operator's verifier key.
    NEVER rotated in this pass; the operator rotates the platform tenant separately
    and last, after verifying the rotation with that key.
  * **excluded test-artefact tenants** (`ROTATE_EXCLUDE_TENANT_IDS`).
  * tenants with **no `tenant_api_keys` rows** (nothing to rotate).

DB URL: `GRAFOMEM_ROTATE_DB_URL`, else `GRAFOMEM_MIGRATE_URL`. This script NEVER
prints an API key value. New keys from a `--live` run are written show-once to a
0600 file (`--out`, default ./rotated_keys.<ts>.jsonl) — deliver them to tenants,
then delete the file.

After a `--live` run the running API still caches key→tenant for ≤60s: RESTART the
API to flush it, then prove the rotation with the must-fail probe (old key → 403
"Invalid API key", new key → 200 on the same endpoint). "rows updated" is not proof.

Fail-closed: refuses to run if PLATFORM_TENANT_IDS is unset (it cannot protect the
verifier key it doesn't know).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import psycopg


def _env_ids(name: str) -> set[str]:
    return {x.strip() for x in os.environ.get(name, "").split(",") if x.strip()}


def _db_url() -> str:
    url = os.environ.get("GRAFOMEM_ROTATE_DB_URL") or os.environ.get("GRAFOMEM_MIGRATE_URL")
    if not url:
        raise SystemExit("set GRAFOMEM_ROTATE_DB_URL (or GRAFOMEM_MIGRATE_URL)")
    return url


def _plan(conn) -> tuple[list[dict], set[str], set[str]]:
    platform = _env_ids("PLATFORM_TENANT_IDS")
    if not platform:
        raise SystemExit(
            "PLATFORM_TENANT_IDS is unset — refusing to run: the platform (verifier) key "
            "must be identified so it is NOT rotated in this pass."
        )
    exclude = _env_ids("ROTATE_EXCLUDE_TENANT_IDS")
    rows = conn.execute(
        "SELECT t.id, t.name, "
        "  (SELECT count(*) FROM tenant_api_keys k WHERE k.tenant_id = t.id) AS n_keys "
        "FROM tenants t ORDER BY t.name"
    ).fetchall()
    plan = []
    for tid, name, n_keys in rows:
        if tid in platform:
            action, reason = "SKIP", "platform/verifier key — rotated separately, last"
        elif tid in exclude:
            action, reason = "SKIP", "excluded test artefact"
        elif not n_keys:
            action, reason = "SKIP", "no tenant_api_keys rows"
        else:
            action, reason = "ROTATE", f"{n_keys} key(s) → 1 new default_admin"
        plan.append({"id": tid, "name": name, "n_keys": n_keys, "action": action, "reason": reason})
    return plan, platform, exclude


def _report(plan, platform, exclude, live: bool) -> None:
    print(f"\n  rotate-all-tenant-keys — {'LIVE' if live else 'DRY-RUN'}")
    print(f"  PLATFORM_TENANT_IDS (protected): {sorted(platform)}")
    print(f"  ROTATE_EXCLUDE_TENANT_IDS:       {sorted(exclude) or '(none)'}")
    print("  " + "-" * 72)
    print(f"  {'action':7} {'keys':>4}  {'tenant_id':32} name")
    for r in plan:
        print(f"  {r['action']:7} {r['n_keys']:>4}  {r['id']:32} {r['name']}")
    rot = [r for r in plan if r["action"] == "ROTATE"]
    skipped = [r for r in plan if r["action"] == "SKIP"]
    print("  " + "-" * 72)
    print(f"  tenants found:        {len(plan)}")
    print(f"  would ROTATE:         {len(rot)} tenants, {sum(r['n_keys'] for r in rot)} key(s) deleted → {len(rot)} new")
    print(f"  SKIP platform:        {[r['id'] for r in plan if r['action']=='SKIP' and 'platform' in r['reason']]}")
    print(f"  SKIP artefact:        {[r['id'] for r in skipped if 'artefact' in r['reason']]}")
    print(f"  SKIP no-keys:         {[r['id'] for r in skipped if 'no tenant_api_keys' in r['reason']]}")


def _rotate_live(url: str, plan, out_path: str) -> None:
    from aml.cloud.tenant_manager import TenantManager
    from aml.server.scopes import TENANT_ADMIN_SCOPES

    tm = TenantManager(url)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    n = 0
    with os.fdopen(fd, "w") as out:
        for r in plan:
            if r["action"] != "ROTATE":
                continue
            conn = tm._get_conn()
            with conn.transaction():
                conn.execute("DELETE FROM tenant_api_keys WHERE tenant_id = %s", (r["id"],))
                res = tm.create_api_key(r["id"], name="default_admin", role="admin",
                                        scopes=TENANT_ADMIN_SCOPES)
            out.write(json.dumps({"tenant_id": r["id"], "name": r["name"],
                                  "api_key": res["api_key"], "key_id": res["key_id"]}) + "\n")
            n += 1
    print(f"\n  ROTATED {n} tenant(s). New keys (show-once) written to {out_path} (0600).")
    print("  NEXT: restart the API to flush the ≤60s auth cache, then run the must-fail probe:")
    print("        old key → 403 'Invalid API key'; new key → 200 on the same endpoint.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Rotate all tenant API keys (dry-run by default).")
    ap.add_argument("--live", action="store_true", help="Perform the rotation (default: dry-run).")
    ap.add_argument("--out", default=f"rotated_keys.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl",
                    help="show-once output file for new keys (--live only; 0600).")
    args = ap.parse_args(argv)
    url = _db_url()
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        plan, platform, exclude = _plan(conn)
    _report(plan, platform, exclude, args.live)
    if not args.live:
        print("\n  DRY-RUN — nothing changed. Re-run with --live (after approval) to rotate.\n")
        return
    _rotate_live(url, plan, args.out)


if __name__ == "__main__":
    main()
