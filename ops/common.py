"""Shared config + tiny HTTP helpers for Ulissy — tenant #1 of Grafomem Cloud (Phase 0).

Mirrors demo/common.py but scoped to the *internal* Ulissy tenant, whose creds live
in a gitignored file separate from the Kapwork demo tenant. Secrets come from the
environment / that creds file — never hardcoded, never committed.

  GRAFOMEM_BASE : base URL. Defaults to prod (this IS the prod internal tenant).
                  Override for local/staging, e.g. http://localhost:8090
  ULISSY_CREDS  : path to the creds JSON written by setup_ulissy_tenant.py
                  (default: ops/.ulissy_creds.json, which is gitignored)
"""
from __future__ import annotations

import json
import os
import sys

import httpx

# This tenant is the production internal tenant, so the default base is prod.
# Every prod-writing entrypoint prints BASE before it acts.
BASE = os.environ.get("GRAFOMEM_BASE", "https://grafomem-production.up.railway.app").rstrip("/")
CREDS_PATH = os.environ.get("ULISSY_CREDS", os.path.join(os.path.dirname(__file__), ".ulissy_creds.json"))


def load_creds() -> dict:
    """Load {tenant_id, api_key, email, agents{...}} written by setup_ulissy_tenant.py."""
    if not os.path.exists(CREDS_PATH):
        sys.exit(f"blocked — no creds file at {CREDS_PATH}. Run: python ops/setup_ulissy_tenant.py")
    with open(CREDS_PATH) as f:
        return json.load(f)


def save_creds(creds: dict) -> None:
    with open(CREDS_PATH, "w") as f:
        json.dump(creds, f, indent=2)
    os.chmod(CREDS_PATH, 0o600)


def client(api_key: str | None = None, timeout: float = 30.0) -> httpx.Client:
    headers = {"X-API-Key": api_key} if api_key else {}
    return httpx.Client(base_url=BASE, headers=headers, timeout=timeout)


def redact_key(k: str) -> str:
    return (k[:6] + "…" + k[-4:]) if k and len(k) > 12 else "<set>"
