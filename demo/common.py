"""Shared config + tiny HTTP helpers for the Kapwork x GRAFOMEM demo.

Secrets come from the environment / a gitignored creds file — never hardcoded.
  GRAFOMEM_BASE : base URL (e.g. http://localhost:8090 for local, or the staging URL)
  DEMO_CREDS    : path to the creds JSON written by setup_tenant.py
                  (default: demo/.demo_creds.json, which is gitignored)
"""
from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.environ.get("GRAFOMEM_BASE", "http://localhost:8090").rstrip("/")
CREDS_PATH = os.environ.get("DEMO_CREDS", os.path.join(os.path.dirname(__file__), ".demo_creds.json"))


def load_creds() -> dict:
    """Load {tenant_id, api_key, email} written by setup_tenant.py."""
    if not os.path.exists(CREDS_PATH):
        sys.exit(f"blocked — no creds file at {CREDS_PATH}. Run: python demo/setup_tenant.py")
    with open(CREDS_PATH) as f:
        return json.load(f)


def save_creds(creds: dict) -> None:
    with open(CREDS_PATH, "w") as f:
        json.dump(creds, f, indent=2)
    os.chmod(CREDS_PATH, 0o600)


def client(api_key: str | None = None) -> httpx.Client:
    headers = {"X-API-Key": api_key} if api_key else {}
    return httpx.Client(base_url=BASE, headers=headers, timeout=30.0)


def redact_key(k: str) -> str:
    return (k[:6] + "…" + k[-4:]) if k and len(k) > 12 else "<set>"
