#!/usr/bin/env python3
"""Rotate / mint / revoke tenant API keys — **scope-preserving**. DRY-RUN by default.

Three modes:

1. **Rotate-all (default)** — for EVERY `tenant_api_keys` row (except excluded tenants), mint a
   replacement carrying the **exact** `scopes` and every other non-secret column, then **delete**
   the old row (1:1 re-mint). Platform tenants (`PLATFORM_TENANT_IDS`) are SKIPped.

2. **`--only-tenant <id>` (repeatable)** — mint **alongside** (NO delete) for the named tenant(s).
   The platform-tenant protection is bypassed **only** for the id(s) the operator typed; every
   other platform tenant still SKIPs. The old key keeps working until the operator revokes it with
   `--revoke-key` (coexistence lets consumers cut over first). By default each existing key is
   copied 1:1 (scopes byte-identical). With `--scopes`/`--name`/`--role`, mints ONE fresh key with
   the given fields instead (e.g. a least-privilege CI key) — requires a single `--only-tenant`.

3. **`--revoke-key <key_id>`** — delete exactly one `tenant_api_keys` row by `key_id`. Use this to
   retire a superseded key AFTER its consumers carry the replacement.

`tenant_api_keys` columns (staging/prod, authoritative):
    key_id, tenant_id, api_key, name, role, scopes, allowed_stores, expires_at,
    last_used_at, ip_allowlist, is_service_account, created_at

`role` is preserved deliberately: an empty `scopes` ({}) resolves at check time to the role's
default scopes (auth.py `if db_scopes: … else: scopes = ROLE_SCOPES.get(role, ["*"])`), so a {} key's
effective authority depends on `role`. A key minted WITH explicit `--scopes` uses exactly those.

DB URL: `GRAFOMEM_ROTATE_DB_URL`, else `GRAFOMEM_MIGRATE_URL`. NEVER prints an api_key. `--live`
writes new keys show-once to a 0600 file. After a `--live` mint/rotate: restart the API (flush the
≤60s auth cache), then probe (assert the response BODY, curl UA — CDN status alone is unreliable):
new key `GET /v1/usage/current` → 200; a revoked/old key → 403 with body `Invalid API key.`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

# Non-secret columns carried verbatim from the replaced row (1:1 preserve path).
_PRESERVED = ("tenant_id", "name", "role", "scopes", "allowed_stores",
              "expires_at", "ip_allowlist", "is_service_account", "created_at")

_KEY_SELECT = (
    "SELECT k.key_id, k.tenant_id, t.name AS tenant_name, k.name, k.role, k.scopes, "
    "       k.allowed_stores, k.expires_at, k.ip_allowlist, k.is_service_account, k.created_at "
    "FROM tenant_api_keys k JOIN tenants t ON t.id = k.tenant_id "
)


def _env_ids(name: str) -> set[str]:
    return {x.strip() for x in os.environ.get(name, "").split(",") if x.strip()}


def _db_url() -> str:
    url = os.environ.get("GRAFOMEM_ROTATE_DB_URL") or os.environ.get("GRAFOMEM_MIGRATE_URL")
    if not url:
        raise SystemExit("set GRAFOMEM_ROTATE_DB_URL (or GRAFOMEM_MIGRATE_URL)")
    return url


def _fmt_scopes(s) -> str:
    if not s:
        return "{}"
    return "{" + ",".join(s) + "}"


def _tenant_name(conn, tid: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM tenants WHERE id = %s", (tid,))
        row = cur.fetchone()
        return row[0] if row else None


# ── Mode 1: rotate-all (default, delete-based) ──────────────────────────────

def _plan_all(conn) -> tuple[list[dict], set[str], set[str]]:
    platform = _env_ids("PLATFORM_TENANT_IDS")
    if not platform:
        raise SystemExit(
            "PLATFORM_TENANT_IDS is unset — refusing to run: the platform (verifier) key(s) must be "
            "identified so they are NOT rotated in this pass."
        )
    exclude = _env_ids("ROTATE_EXCLUDE_TENANT_IDS")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_KEY_SELECT + "ORDER BY t.name, k.created_at")
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


def _report_all(plan, platform, exclude, live: bool) -> None:
    print(f"\n  rotate-all-tenant-keys v2 (scope-preserving 1:1, delete-based) — {'LIVE' if live else 'DRY-RUN'}")
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


def _rotate_all_live(conn, plan, out_path: str) -> int:
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


# ── Mode 2: --only-tenant (mint alongside, NO delete) ───────────────────────

def _plan_only(conn, only_tenants: list[str]) -> list[dict]:
    """Existing key rows for the named tenants (1:1 mint-alongside sources).

    Refuses if a named tenant does not exist. A named tenant with no key rows is only valid in
    scoped-mint mode (caller checks); here it simply yields no source rows.
    """
    for tid in only_tenants:
        if _tenant_name(conn, tid) is None:
            raise SystemExit(f"--only-tenant {tid!r}: no such tenant (tenants.id)")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_KEY_SELECT + "WHERE k.tenant_id = ANY(%s) ORDER BY t.name, k.created_at",
                    (list(only_tenants),))
        return cur.fetchall()


def _report_only(only_tenants, rows, override, live: bool) -> None:
    mode = "scoped fresh mint" if override else "1:1 copy (scopes preserved)"
    print(f"\n  mint-alongside (NO delete) — {'LIVE' if live else 'DRY-RUN'}")
    print(f"  --only-tenant: {only_tenants}   mode: {mode}")
    if override:
        print(f"  new key → name={override['name']!r} role={override['role']!r} "
              f"scopes={_fmt_scopes(override['scopes'])}")
        print(f"  tenants targeted: {len(only_tenants)} (one fresh key each)")
    else:
        print("  " + "-" * 96)
        print(f"  {'source key':10} {'role':10} {'scopes':40} tenant   → alongside copy")
        for r in rows:
            print(f"  {r['key_id'][:8]:10} {(r['role'] or ''):10} {_fmt_scopes(r['scopes'])[:40]:40} "
                  f"{r['tenant_name']}")
        print("  " + "-" * 96)
        print(f"  would MINT alongside: {len(rows)} key(s); old key(s) kept (revoke later with --revoke-key)")


def _mint_only_live(conn, only_tenants, rows, override, out_path: str) -> int:
    from aml.cloud.tenant_manager import _generate_api_key
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    n = 0
    now = datetime.now(tz=timezone.utc)
    with os.fdopen(fd, "w") as out:
        if override:
            # One fresh, explicitly-scoped key per named tenant. No source row copied.
            for tid in only_tenants:
                tname = _tenant_name(conn, tid)
                new_key = _generate_api_key(role=override["role"], is_service_account=False)
                new_key_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO tenant_api_keys "
                    "(key_id, tenant_id, api_key, name, role, scopes, allowed_stores, "
                    " expires_at, ip_allowlist, is_service_account, created_at, last_used_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'{}',NULL,'{}',false,%s,NULL)",
                    (new_key_id, tid, new_key, override["name"], override["role"],
                     override["scopes"], now),
                )
                out.write(json.dumps({
                    "tenant_id": tid, "tenant_name": tname, "new_key_id": new_key_id,
                    "name": override["name"], "role": override["role"],
                    "api_key": new_key, "scopes": override["scopes"],
                }) + "\n")
                n += 1
        else:
            # 1:1 alongside copy of each existing key (scopes byte-identical). Old rows KEPT.
            for r in rows:
                new_key = _generate_api_key(role=r["role"] or "admin",
                                            is_service_account=bool(r["is_service_account"]))
                new_key_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO tenant_api_keys "
                    "(key_id, tenant_id, api_key, name, role, scopes, allowed_stores, "
                    " expires_at, ip_allowlist, is_service_account, created_at, last_used_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)",
                    (new_key_id, r["tenant_id"], new_key, r["name"], r["role"], r["scopes"],
                     r["allowed_stores"], r["expires_at"], r["ip_allowlist"],
                     r["is_service_account"], now),
                )
                out.write(json.dumps({
                    "tenant_id": r["tenant_id"], "tenant_name": r["tenant_name"],
                    "source_key_id": r["key_id"], "new_key_id": new_key_id,
                    "api_key": new_key, "scopes": r["scopes"] or [],
                }) + "\n")
                n += 1
    return n


# ── Mode 3: --revoke-key <key_id> ───────────────────────────────────────────

def _revoke(conn, key_id: str, live: bool) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_KEY_SELECT + "WHERE k.key_id = %s", (key_id,))
        row = cur.fetchone()
    if row is None:
        raise SystemExit(f"--revoke-key {key_id!r}: no such tenant_api_keys row")
    print(f"\n  revoke-key — {'LIVE' if live else 'DRY-RUN'}")
    print(f"  key_id={row['key_id'][:8]}… tenant={row['tenant_name']} "
          f"role={row['role']} scopes={_fmt_scopes(row['scopes'])} name={row['name']!r}")
    # Guard: never let a revoke strand a tenant with zero keys unless explicitly the intent.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tenant_api_keys WHERE tenant_id = %s", (row["tenant_id"],))
        remaining = cur.fetchone()[0] - 1
    print(f"  tenant would have {remaining} key(s) left after this revoke")
    if remaining <= 0:
        print("  ⚠ WARNING: this is the tenant's LAST key — it would have no working credential.")
    if not live:
        print("\n  DRY-RUN — nothing deleted. Re-run with --live to revoke.\n")
        return
    conn.execute("DELETE FROM tenant_api_keys WHERE key_id = %s", (key_id,))
    print(f"\n  REVOKED key_id {key_id[:8]}…. NEXT: restart the API (flush the ≤60s auth cache), then "
          "probe the revoked key → 403 body 'Invalid API key.'\n")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Rotate / mint / revoke tenant API keys (dry-run default).")
    ap.add_argument("--live", action="store_true", help="Perform the change (default: dry-run).")
    ap.add_argument("--only-tenant", action="append", default=[], metavar="TENANT_ID",
                    help="Mint ALONGSIDE (no delete) for this tenant; repeatable. Bypasses platform "
                         "protection only for the id(s) named.")
    ap.add_argument("--revoke-key", metavar="KEY_ID", help="Delete exactly one tenant_api_keys row by key_id.")
    ap.add_argument("--name", help="(--only-tenant) name for a freshly-scoped minted key.")
    ap.add_argument("--role", default="agent", help="(--only-tenant + --scopes) role for the minted key (default: agent).")
    ap.add_argument("--scopes", help="(--only-tenant) comma-separated scopes for a FRESH scoped key "
                                     "(e.g. 'cgr:read'). Omit to copy existing keys 1:1.")
    ap.add_argument("--out", default=None, help="show-once output for new keys (--live mint modes; 0600).")
    args = ap.parse_args(argv)

    if args.revoke_key and args.only_tenant:
        raise SystemExit("--revoke-key and --only-tenant are mutually exclusive")

    url = _db_url()
    out_path = args.out or f"minted_keys.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"

    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        # ── Mode 3: revoke ──
        if args.revoke_key:
            if args.live:
                conn.autocommit = False
            _revoke(conn, args.revoke_key, args.live)
            if args.live:
                conn.commit()
            return

        # ── Mode 2: --only-tenant (mint alongside) ──
        if args.only_tenant:
            override = None
            if args.scopes is not None:
                if len(args.only_tenant) != 1:
                    raise SystemExit("--scopes (fresh scoped mint) requires exactly one --only-tenant")
                if not args.name:
                    raise SystemExit("--scopes requires --name for the minted key")
                scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
                if not scopes:
                    raise SystemExit("--scopes was empty")
                override = {"name": args.name, "role": args.role, "scopes": scopes}
            rows = _plan_only(conn, args.only_tenant)
            if override is None and not rows:
                raise SystemExit(
                    f"--only-tenant {args.only_tenant}: tenant has no existing keys to copy 1:1; "
                    "pass --scopes/--name to mint a fresh scoped key instead")
            _report_only(args.only_tenant, rows, override, args.live)
            if not args.live:
                print("\n  DRY-RUN — nothing changed. Re-run with --live (after approval) to mint.\n")
                return
            conn.autocommit = False
            n = _mint_only_live(conn, args.only_tenant, rows, override, out_path)
            conn.commit()
            print(f"\n  MINTED {n} key(s) ALONGSIDE (old key(s) kept). Show-once → {out_path} (0600).")
            print("  NEXT: point the consumer at the new key, restart/redeploy, probe new → 200; then "
                  "retire the old key with --revoke-key <old_key_id>.")
            return

        # ── Mode 1: rotate-all (default) ──
        plan, platform, exclude = _plan_all(conn)
        _report_all(plan, platform, exclude, args.live)
        if not args.live:
            print("\n  DRY-RUN — nothing changed. Re-run with --live (after approval) to rotate.\n")
            return
        conn.autocommit = False
        n = _rotate_all_live(conn, plan, out_path)
    print(f"\n  ROTATED {n} key(s), 1:1 scope-preserving (delete-based). New keys (show-once) → {out_path} (0600).")
    print("  NEXT: restart the API (flush the ≤60s auth cache), then probe:")
    print("        old key → 403 'Invalid API key'; new key → 200; scopes byte-identical before/after.")


if __name__ == "__main__":
    main()
