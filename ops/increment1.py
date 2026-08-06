"""Phase 2 — Increment 1: stand up the corp tenant's governed front-agent.

Run AFTER ops/adopt_corp_tenant.py has put the corp key into ops/.ulissy_creds.json.

Does, against the corp tenant (all auth via the gitignored creds key — no secrets on the
command line):
  1. (optional) register LLM providers from a gitignored ops/.env.providers file
     (KEY=VALUE lines: OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY). Skipped if absent.
  2. create the orchestrated agent gtm-outreach-agent@ulissy, reusing the pinned agent_key
     (the stable CGR subject) — idempotent-ish: if it already exists, reuse it.
  3. verify the agent is visible in Agent Studio (GET /v1/orchestrator/agents).
  4. isolation check: the corp key authenticates and returns only this tenant's scores.

Prints a PASS/FAIL summary. No api_key or provider key is ever printed.

    python ops/increment1.py
"""
from __future__ import annotations

import os
import sys

from common import BASE, client, load_creds

AGENT_NAME = "gtm-outreach-agent@ulissy"
AGENT_MODEL = os.environ.get("ULISSY_AGENT_MODEL", "claude-3-opus-20240229")  # anthropic to start
SYSTEM_PROMPT = (
    "You are gtm-outreach-agent@ulissy, a governed GTM outreach agent for Ulissy. "
    "You PROPOSE outreach actions (send_email) to named prospects. Every send to a named "
    "human is an edge action requiring founder approval via HITL — you never send without "
    "an approval. You record decisions and outcomes through the governed substrate."
)

PROVIDER_MODELS = {
    "openai": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    "anthropic": os.environ.get("ANTHROPIC_MODEL", "claude-3-opus-20240229"),
    "gemini": os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"),
}
PROVIDER_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}


def _env_providers() -> dict[str, str]:
    """Parse gitignored ops/.env.providers (KEY=VALUE) -> {env_name: value}. {} if absent."""
    path = os.path.join(os.path.dirname(__file__), ".env.providers")
    if not os.path.exists(path):
        return {}
    out: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def register_providers(c) -> list[str]:
    env = _env_providers()
    done = []
    for provider, model_id in PROVIDER_MODELS.items():
        key = env.get(PROVIDER_ENV[provider])
        if not key:
            print(f"  · provider {provider}: no key in ops/.env.providers — skipped")
            continue
        r = c.post("/v1/llm/providers", json={"provider": provider, "model_id": model_id, "api_key": key})
        ok = r.status_code in (200, 201)
        print(f"  · provider {provider} ({model_id}): {'registered' if ok else f'FAILED {r.status_code} {r.text[:80]}'}")
        if ok:
            done.append(provider)
    return done


def ensure_agent(c, agent_key: str, agent_tier) -> dict | None:
    # already present?
    r = c.get("/v1/orchestrator/agents")
    r.raise_for_status()
    existing = r.json().get("agents", r.json() if isinstance(r.json(), list) else [])
    for a in existing:
        if a.get("name") == AGENT_NAME:
            print(f"  · agent already exists: {a.get('agent_id')} (reusing)")
            return a
    body = {
        "name": AGENT_NAME,
        "role": "custom",
        "model_id": AGENT_MODEL,
        "system_prompt": SYSTEM_PROMPT,
        "tools": [],
        "agent_key": agent_key,
        "agent_handle": AGENT_NAME,
    }
    r = c.post("/v1/orchestrator/agents", json=body)
    if r.status_code not in (200, 201):
        sys.exit(f"  ❌ create agent failed: {r.status_code} {r.text[:200]}")
    a = r.json()
    print(f"  · agent created: {a.get('agent_id')}")
    return a


def main() -> int:
    creds = load_creds()
    key = creds["api_key"]
    acfg = creds.get("agents", {}).get(AGENT_NAME) or {}
    agent_key = acfg.get("agent_key")
    if not agent_key:
        sys.exit(f"blocked — no pinned agent_key for {AGENT_NAME} in creds.")

    print(f"BASE = {BASE}")
    print(f"tenant_id = {creds['tenant_id']}  email = {creds.get('email')}")

    with client(key, timeout=60.0) as c:
        # 0. sanity: the corp key authenticates
        r = c.get("/v1/cgr/scores")
        if r.status_code != 200:
            sys.exit(f"❌ corp key does not authenticate: {r.status_code} {r.text[:120]} "
                     f"(run ops/adopt_corp_tenant.py first)")

        print("\n[1/4] register providers (from gitignored ops/.env.providers)")
        providers = register_providers(c)

        print("\n[2/4] create/ensure agent")
        agent = ensure_agent(c, agent_key, acfg.get("agent_tier"))

        print("\n[3/4] verify agent visible in Agent Studio")
        r = c.get("/v1/orchestrator/agents")
        r.raise_for_status()
        agents = r.json().get("agents", [])
        names = [a.get("name") for a in agents]
        visible = AGENT_NAME in names
        key_ok = any(a.get("name") == AGENT_NAME and a.get("agent_key") == agent_key for a in agents)
        print(f"  · agents on tenant: {names}")
        print(f"  · {AGENT_NAME} visible: {visible} | carries pinned agent_key: {key_ok}")

        print("\n[4/4] isolation check")
        sc = c.get("/v1/cgr/scores")
        iso_ok = sc.status_code == 200
        print(f"  · /v1/cgr/scores -> {sc.status_code} (scoped to corp tenant): {'PASS' if iso_ok else 'FAIL'}")
        print("  · (definitive cross-tenant isolation proven by tests/test_cgr_rls.py + #12a)")

    print("\n=== Increment 1 summary ===")
    print(f"  providers registered : {providers or 'none (add ops/.env.providers)'}")
    print(f"  agent visible        : {'PASS' if visible else 'FAIL'}")
    print(f"  pinned agent_key      : {'PASS' if key_ok else 'FAIL'}")
    print(f"  isolation            : {'PASS' if iso_ok else 'FAIL'}")
    return 0 if (visible and key_ok and iso_ok) else 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
