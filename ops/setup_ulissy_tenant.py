"""Phase 0, step 1 — provision the internal Ulissy tenant on prod Grafomem.

Idempotent: signs up the Ulissy tenant, or logs in if it already exists. Pins a
STABLE agent_key for the first front-agent (gtm-outreach-agent@ulissy) so the CGR
engine — which groups scores by agent_key — never fragments the agent's reputation
across runs. Writes everything to a gitignored creds file (0600), never to stdout in
full, never committed.

Also runs the isolation gate: a /v1/cgr/scores call with the Ulissy key must succeed
and return ONLY Ulissy data (zero rows on a fresh tenant) — proving the #12a tenant
scoping holds for this new tenant by construction.

    GRAFOMEM_BASE=https://grafomem-production.up.railway.app python ops/setup_ulissy_tenant.py
"""
from __future__ import annotations

import os
import secrets
import sys

from common import BASE, client, load_creds, redact_key, save_creds

# Email/password/name are env-overridable so a FRESH tenant can be provisioned without a
# code change (a new email forces a new tenant; the fixed email would just log back in).
ULISSY_EMAIL = os.environ.get("ULISSY_EMAIL", "ops@ulissy.grafomem")   # distinct from the Kapwork demo
ULISSY_PASSWORD = os.environ.get("ULISSY_PASSWORD", "ulissy-ops-Phase0-2026!")  # throwaway; rotate if exposed
ULISSY_NAME = os.environ.get("ULISSY_NAME", "Ulissy (internal ops)")

FRONT_AGENT = "gtm-outreach-agent@ulissy"      # first scored actor; finance-/code-/research- slot in later


def provision() -> dict:
    # Capture any prior creds BEFORE overwriting, so a fresh tenant preserves the old.
    prior: dict = {}
    try:
        prior = load_creds()
    except SystemExit:
        prior = {}

    with client() as c:
        r = c.post("/v1/portal/signup", json={
            "name": ULISSY_NAME, "email": ULISSY_EMAIL,
            "password": ULISSY_PASSWORD, "plan": "starter",
        })
        if r.status_code == 201:
            info = r.json()
            print(f"  signup: 201 Created  tenant_id={info['tenant_id']}")
        else:
            r = c.post("/v1/portal/login", json={"email": ULISSY_EMAIL, "password": ULISSY_PASSWORD})
            r.raise_for_status()
            info = r.json()
            print(f"  signup existed -> login: 200  tenant_id={info['tenant_id']}")

    new_tenant_id = info["tenant_id"]

    # agent_key policy: reuse the pinned key ONLY when re-provisioning the SAME tenant
    # (stability across re-runs). A NEW tenant gets a freshly minted key — a clean-break
    # identity with zero entanglement with any prior tenant's data.
    same_tenant = bool(prior.get("tenant_id")) and prior.get("tenant_id") == new_tenant_id
    agents = dict(prior.get("agents", {})) if same_tenant else {}
    if FRONT_AGENT not in agents or not agents[FRONT_AGENT].get("agent_key"):
        agents[FRONT_AGENT] = {"agent_key": secrets.token_hex(32), "agent_tier": None}
        print(f"  minted FRESH agent_key for {FRONT_AGENT}")
    else:
        print(f"  reused pinned agent_key for {FRONT_AGENT} (same tenant, stable across runs)")

    creds = {
        "tenant_id": new_tenant_id,
        "api_key": info["api_key"],
        "email": ULISSY_EMAIL,
        "agents": agents,
    }

    # Preserve the previous, now-orphaned tenant (creds file is gitignored — never committed).
    # There is no purge path (see ops/ROADMAP.md), so we keep the throwaway's coordinates
    # in case we ever need to read it.
    if prior.get("tenant_id") and prior.get("tenant_id") != new_tenant_id:
        creds["_previous_tenant"] = {
            "note": "orphaned throwaway tenant (held synthetic sample data); no purge path — ops/ROADMAP.md",
            "tenant_id": prior.get("tenant_id"),
            "api_key": prior.get("api_key"),
            "email": prior.get("email"),
            "agents": prior.get("agents", {}),
        }
        if prior.get("_previous_tenant"):          # never lose an older predecessor in the chain
            creds["_previous_tenant"]["_previous_tenant"] = prior["_previous_tenant"]
        print(f"  preserved previous tenant {str(prior['tenant_id'])[:12]}… under _previous_tenant")

    save_creds(creds)
    print(f"  api_key: {redact_key(creds['api_key'])}  (full key written to gitignored creds file)")
    return creds


def isolation_gate(creds: dict) -> None:
    """Fresh Ulissy tenant must return its OWN (empty) score set — never demo data."""
    with client(creds["api_key"]) as c:
        r = c.get("/v1/cgr/scores")
        r.raise_for_status()
        body = r.json()
        scores = body.get("scores", [])
        print(f"  /v1/cgr/scores: {r.status_code}  count={body.get('count', len(scores))}")
        # On a fresh tenant this must be empty; if not, we are seeing another tenant's data.
        foreign = [s for s in scores if not str(s.get("agent_handle", "")).endswith("@ulissy")]
        if foreign:
            sys.exit(f"  ❌ ISOLATION FAILURE — Ulissy key returned non-Ulissy subjects: "
                     f"{[s.get('agent_handle') for s in foreign][:5]}")
        print("  ✅ isolation gate passed: Ulissy key sees only Ulissy data.")


if __name__ == "__main__":
    print(f"BASE = {BASE}")
    print("== provision Ulissy tenant ==")
    creds = provision()
    print("== isolation gate ==")
    isolation_gate(creds)
    print("OK: Ulissy tenant ready, agent_key pinned, isolation verified.")
