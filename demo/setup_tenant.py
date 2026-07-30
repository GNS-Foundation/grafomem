"""Task 1 — provision the isolated demo tenant + create->write->retrieve smoke test.

Idempotent: signs up the demo tenant, or logs in if it already exists. Writes the
api_key to a gitignored creds file (never printed to stdout in full, never committed).
"""
from __future__ import annotations

import sys

from common import BASE, client, load_creds, redact_key, save_creds

DEMO_EMAIL = "demo-tenant@kapwork-demo.example"
DEMO_PASSWORD = "demo-Kapwork-2026!"   # synthetic demo credential, throwaway tenant
DEMO_NAME = "Kapwork Demo (synthetic)"


def provision() -> dict:
    with client() as c:
        r = c.post("/v1/portal/signup", json={
            "name": DEMO_NAME, "email": DEMO_EMAIL,
            "password": DEMO_PASSWORD, "plan": "starter",
        })
        if r.status_code == 201:
            info = r.json()
            print(f"  signup: 201 Created  tenant_id={info['tenant_id']}")
        else:
            r = c.post("/v1/portal/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
            r.raise_for_status()
            info = r.json()
            print(f"  signup existed -> login: 200  tenant_id={info['tenant_id']}")
    creds = {"tenant_id": info["tenant_id"], "api_key": info["api_key"], "email": DEMO_EMAIL}
    save_creds(creds)
    print(f"  api_key: {redact_key(creds['api_key'])}  (full key written to gitignored creds file)")
    return creds


def smoke_test(creds: dict) -> None:
    with client(creds["api_key"]) as c:
        r = c.post("/v1/stores", json={})
        r.raise_for_status()
        store_id = r.json()["store_id"]
        print(f"  create store: {r.status_code}  store_id={store_id}")

        fact = "Kapwork demo tenant is live (synthetic)."
        r = c.post(f"/v1/stores/{store_id}/write", json={"content": fact})
        r.raise_for_status()
        ref = r.json()["ref"]
        print(f"  write fact:   {r.status_code}  ref={ref}")

        r = c.post(f"/v1/stores/{store_id}/retrieve", json={"query": "is the demo tenant live?"})
        r.raise_for_status()
        mems = r.json()["memories"]
        print(f"  retrieve:     {r.status_code}  memories={len(mems)}  first={mems[0]['content'] if mems else None!r}")


if __name__ == "__main__":
    print(f"BASE = {BASE}")
    print("== provision demo tenant ==")
    creds = provision()
    print("== create -> write -> retrieve smoke test ==")
    smoke_test(creds)
    print("OK: demo tenant ready and smoke test passed.")
