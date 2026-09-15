#!/usr/bin/env python3
"""Precautionary rotate-all-tenant-keys — **scope-preserving 1:1 re-mint**. DRY-RUN by default.

For EVERY existing `tenant_api_keys` row (except excluded tenants), mint a replacement that carries the
**exact** `scopes` and every other non-secret column of the row it replaces, then delete the old row.
Only the secret (`api_key`) and its `key_id` change. This preserves narrow-scope keys and multi-key
tenants — unlike a naive "delete all, mint one admin key" rotation, which would widen scopes and collapse
key counts.

`tenant_api_keys` columns (staging/prod, authoritative):
    key_id, tenant_id, api_key, name, role, scopes, allowed_stores, expires_at,
    last_used_at, ip_allowlist, is_service_account, created_at
Per replacement row:
  * REGENERATED: key_id (new uuid), api_key (new gfm_ secret)
  * PRESERVED 1:1: tenant_id, name, role, scopes, allowed_stores, expires_at, ip_allowlist,
    is_service_account, created_at
  * RESET: last_used_at → NULL (a brand-new key has no usage; auth populates it on first use —
    carrying the old value would be false provenance)

`role` is preserved deliberately: an empty `scopes` ({}) resolves at check time to the role's default
scopes (auth.py `if db_scopes: … else: scopes = ROLE_SCOPES.get(role, ["*"])`), so a {} key's effective
authority depends on `role`; preserving both keeps behavior byte-identical.

Excludes: platform tenants (`PLATFORM_TENANT_IDS` — the operator's verifier key, rotated separately/last;
fail-closed if unset) and `ROTATE_EXCLUDE_TENANT_IDS`.

DB URL: `GRAFOMEM_ROTATE_DB_URL`, else `GRAFOMEM_MIGRATE_URL`. NEVER prints an api_key. `--live` writes new
keys show-once to a 0600 file. After `--live`: restart the API (flush ≤60s auth cache), then the must-fail
probe (old key → 403 "Invalid API key"; new key → 200; scopes byte-identical before/after).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import psycopg

# Non-secret columns carried verbatim from the replaced row.
_PRESERVED = ("tenant_id", "name", "role", "scopes", "allowed_stores",
              "expires_at", "ip_allowlist", "is_service_account", "created_at")


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
            "PLATFORM_TENANT_IDS is unset — refusing to run: the platform (verifier) key(s) must be "
            "identified so they are NOT rotated in this pass."
        )
    exclude = _env_ids("ROTATE_EXCLUDE_TENANT_IDS")
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT k.key_id, k.tenant_id, t.name AS tenant_name, k.name, k.role, k.scopes, "
            "       k.allowed_stores, k.expires_at, k.ip_allowlist, k.is_service_account, k.created_at "
            "FROM tenant_api_keys k JOIN tenants t ON t.id = k.tenant_id "
            "ORDER BY t.name, k.created_at"
        )
        rows = cur.fetchall()
    plan = []
    for r in rows:
        if r["tenant_id"] in platform:
            action, reason = "SKIP", "platform/verifier — rotated separately, last"
        elif r["tenant_id"] in exclude:
            action, reason = "SKIP", "excluded"
        else:
            action, reason = "ROTATE", "1:1 re-mint (scopes preserved)"
        r["action"], r["reason"] = action, reason
        plan.append(r)
    return plan, platform, exclude


def _fmt_scopes(s) -> str:
    if not s:
        return "{}"
    return "{" + ",".join(s) + "}"


def _report(plan, platform, exclude, live: bool) -> None:
    print(f"\n  rotate-all-tenant-keys v2 (scope-preserving 1:1) — {'LIVE' if live else 'DRY-RUN'}")
    print(f"  PLATFORM_TENANT_IDS (protected): {sorted(platform)}")
    print(f"  ROTATE_EXCLUDE_TENANT_IDS:       {sorted(exclude) or '(none)'}")
    print(f"  preserved cols: {', '.join(_PRESERVED)}  |  regenerated: key_id, api_key  |  reset: last_used_at")
    print("  " + "-" * 96)
    print(f"  {'action':7} {'key_id':10} {'role':10} {'scopes':40} tenant")
    for r in plan:
        print(f"  {r['action']:7} {r['key_id'][:8]:10} {(r['role'] or ''):10} "
              f"{_fmt_scopes(r['scopes'])[:40]:40} {r['tenant_name']}")
    rot = [r for r in plan if r["action"] == "ROTATE"]
    print("  " + "-" * 96)
    print(f"  keys total:   {len(plan)}")
    print(f"  would ROTATE: {len(rot)} keys across {len({r['tenant_id'] for r in rot})} tenants")
    print(f"  SKIP:         {len(plan)-len(rot)} keys "
          f"({len({r['tenant_id'] for r in plan if r['action']=='SKIP'})} tenants)")


def _rotate_live(conn, plan, out_path: str) -> int:
    from aml.cloud.tenant_manager import _generate_api_key
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    n = 0
    with os.fdopen(fd, "w") as out:
        for r in plan:
            if r["action"] != "ROTATE":
                continue
            new_key = _generate_api_key(role=r["role"] or "admin",
                                        is_service_account=bool(r["is_service_account"]))
            new_key_id = uuid.uuid4().hex
            with conn.transaction():
                conn.execute(
                    "INSERT INTO tenant_api_keys "
                    "(key_id, tenant_id, api_key, name, role, scopes, allowed_stores, "
                    " expires_at, ip_allowlist, is_service_account, created_at, last_used_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)",
                    (new_key_id, r["tenant_id"], new_key, r["name"], r["role"], r["scopes"],
                     r["allowed_stores"], r["expires_at"], r["ip_allowlist"],
                     r["is_service_account"], r["created_at"]),
                )
                conn.execute("DELETE FROM tenant_api_keys WHERE key_id = %s", (r["key_id"],))
            out.write(json.dumps({
                "tenant_id": r["tenant_id"], "tenant_name": r["tenant_name"],
                "old_key_id": r["key_id"], "new_key_id": new_key_id,
                "api_key": new_key, "scopes": r["scopes"] or [],
            }) + "\n")
            n += 1
    return n


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Rotate all tenant API keys, 1:1 scope-preserving (dry-run default).")
    ap.add_argument("--live", action="store_true", help="Perform the rotation (default: dry-run).")
    ap.add_argument("--out", default=f"rotated_keys.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl",
                    help="show-once output for new keys (--live only; 0600).")
    args = ap.parse_args(argv)
    url = _db_url()
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        plan, platform, exclude = _plan(conn)
        _report(plan, platform, exclude, args.live)
        if not args.live:
            print("\n  DRY-RUN — nothing changed. Re-run with --live (after approval) to rotate.\n")
            return
        conn.autocommit = False
        n = _rotate_live(conn, plan, args.out)
    print(f"\n  ROTATED {n} key(s), 1:1 scope-preserving. New keys (show-once) → {args.out} (0600).")
    print("  NEXT: restart the API (flush the ≤60s auth cache), then probe:")
    print("        old key → 403 'Invalid API key'; new key → 200; scopes byte-identical before/after.")


if __name__ == "__main__":
    main()
