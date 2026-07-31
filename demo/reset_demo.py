"""Task 5 (API-only) — reset the demo to a fresh, clean tenant. No DB access.

Why rotation instead of a wipe: the demo's decision_records and execution_receipts
are append-only, tamper-evident audit records — there is deliberately no API to
delete them (deleting the receipts would undercut the very guarantee the demo
proves). So a rehearsal "reset" provisions a brand-new demo tenant via the portal
API and points the creds file at it: a genuinely clean slate, zero prior state,
no direct database connection required.

Note: the 5/3 batch count is independent of any persisted state — the agent's
duplicate check is per-run — so the counts are stable across rehearsals with or
without reset. Rotation just guarantees each rehearsal starts on an empty tenant.
"""
from __future__ import annotations

import secrets

from common import BASE, CREDS_PATH, client, load_creds, redact_key, save_creds
import os

NAME = "Kapwork Demo (synthetic)"
PASSWORD = "demo-Kapwork-2026!"   # synthetic throwaway credential


def count_decisions(api_key: str) -> int | str:
    with client(api_key) as c:
        r = c.get("/v1/decisions/")
        if r.status_code == 200:
            return r.json().get("count", "?")
    return "?"


def main() -> None:
    print(f"BASE = {BASE}")
    if os.path.exists(CREDS_PATH):
        old = load_creds()
        n = count_decisions(old["api_key"])
        print(f"  before: tenant {old['tenant_id']} has {n} decision(s) on record "
              f"(append-only audit — left intact)")

    # Provision a fresh, empty demo tenant (unique email so signup always creates new).
    email = f"demo-tenant+{secrets.token_hex(4)}@kapwork-demo.example"
    with client() as c:
        r = c.post("/v1/portal/signup", json={
            "name": NAME, "email": email, "password": PASSWORD, "plan": "starter"})
        r.raise_for_status()
        info = r.json()
    creds = {"tenant_id": info["tenant_id"], "api_key": info["api_key"], "email": email}
    save_creds(creds)

    n_new = count_decisions(creds["api_key"])
    print(f"  after:  tenant {creds['tenant_id']} has {n_new} decision(s) — CLEAN slate")
    print(f"  creds now point to the fresh tenant (key {redact_key(creds['api_key'])})")
    print("OK: demo reset to a clean tenant." if n_new == 0 else "WARNING: new tenant not empty")


if __name__ == "__main__":
    main()
