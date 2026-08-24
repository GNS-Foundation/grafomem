#!/usr/bin/env python3
"""grafomem-cgr — capture MCP server for GRAFOMEM Cloud CGR substrate.

Two MCP tools that let a coding agent record its own judgments and their
outcomes against a GRAFOMEM Cloud tenant: **capture now, score later.**

  cgr_record_decision(work_item_id, agent_handle, domain, decision, ...)
        → POST /v1/governed/decisions   (signed decision record + chained receipt)
  cgr_record_outcome(work_item_id, result, source?)
        → POST /v1/governed/outcomes    (append-only fact, joined by work_item_id)

This is a thin client over the EXISTING governed HTTP path — no new data plane,
no scoring change, no new crypto. It writes to your tenant; it does not compute
or serve scores.

INVARIANTS

  (1) Tenant pinning.  The tenant you expect is declared up front
      (GRAFOMEM_CGR_TENANT). If the API key resolves to any other tenant, every
      write is refused — at config load and again on each decision response.
      GRAFOMEM_CGR_FORBIDDEN_TENANTS adds an explicit denylist on top, so a
      tenant you never want captured into (e.g. your production tenant) is
      refused even if it were pinned by mistake.

  (2) Key custody.  The config holds only your tenant credential and a map of
      role handles → public agent_key. The tool caller picks a role HANDLE; the
      server injects that role's key. A caller can never supply an arbitrary key
      or point at another tenant. (v0 uses the public agent_key as the binding
      subject; per-decision proof-of-possession is later hardening.)

  (3) Billing is conscious, not accidental.  Governed decisions meter as real
      usage on your plan. Startup prints the current governed-decision count
      (GET /v1/usage/current) so the meter is never a surprise. Expected volume
      is a handful per day — well inside the free tier's allotment.

  (4) Capture now, score later + never falsely resolve.  The irreversible fields
      (work_item_id join key, role handle, verifiability_tag, domain) are logged
      at decision time. An outcome result with no scored mapping is a NO-OP (the
      decision is left PENDING) — falsely resolving a decision corrupts the score.

v0 SCOPE — single-dimension SCORING, but domain is stored DURABLY:
  CGR *scoring* is single-dimension today; all judgment-certify decisions score
  under one dimension. Per-domain *scoring* is later work on the served surface.
  BUT the domain string IS captured durably right now: `domain` is sent as a
  dedicated field and GRAFOMEM stores it server-side, per decision, in the
  never-encrypted, CGR-readable decision `parameters` as `cgr_domain`. So
  per-domain re-scoring later is true, not a hope — the domain lives in the
  signed decision record, not in any client-side log.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

__version__ = "0.1.0"

# A browser-ish UA — the prod edge (Cloudflare) blocks default urllib UAs on these paths.
_UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

# Locked dev-loop domain taxonomy. Captured durably; scored single-dimension in v0.
DEV_DOMAINS = ("deploy-verification", "security-scan", "adversarial-review")

# Outcome result → CGR vocab. Scoring counts ONLY 'paid' (α/success) and 'default'
# (β/failure), and ONLY for decision=certify + verifiability_tag=judgment.
# Anything unmapped ⇒ NO-OP ⇒ left pending.
DEV_OUTCOME_MAP = {
    # positive → 'paid' (the judgment proved out)
    "deploy_succeeded": "paid", "deploy_healthy": "paid", "ci_passed": "paid",
    "migration_applied": "paid", "merge_landed": "paid", "pr_merged": "paid",
    "scan_clean": "paid", "no_vuln_confirmed": "paid",
    "review_confirmed": "paid", "finding_correct": "paid", "bug_confirmed": "paid",
    # negative → 'default' (the judgment failed / was undone)
    "deploy_failed": "default", "deploy_rolled_back": "default",
    "migration_failed": "default", "ci_failed": "default", "merge_reverted": "default",
    "vuln_found": "default", "secret_found": "default",
    "review_refuted": "default", "finding_wrong": "default", "bug_not_real": "default",
}


def map_dev_outcome(result: str) -> str | None:
    """Map a dev result label onto the CGR vocab: 'paid' (α), 'default' (β), or None
    (no scored mapping ⇒ do NOT post ⇒ decision stays pending). See invariant 4."""
    return DEV_OUTCOME_MAP.get(result)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assert_not_forbidden(tenant_id: str, forbidden: set[str], *, where: str) -> None:
    """Refuse a tenant on the explicit denylist (invariant 1). CODE, not a comment."""
    if tenant_id in forbidden:
        raise SystemExit(
            f"REFUSING: {where} is on GRAFOMEM_CGR_FORBIDDEN_TENANTS ({tenant_id}). "
            "This capture loop must never write to that tenant — it would pollute its "
            "substrate. Point it at your capture tenant.")


# ============================================================================
# Config — env-driven. Holds ONLY your tenant credential + role public keys.
# ============================================================================

class Config:
    """Capture config loaded from the environment (invariant 2).

    Env:
      GRAFOMEM_API                 base URL (default https://api.grafomem.com)
      GRAFOMEM_CGR_TENANT_KEY      your tenant's X-API-Key (the sensitive secret)
      GRAFOMEM_CGR_TENANT          the tenant_id you expect that key to resolve to
      GRAFOMEM_CGR_ROLE_KEYS       path to JSON { "<handle>": "<agent_key hex>", ... }
      GRAFOMEM_CGR_ROLE_KEYS_JSON  the same mapping inline, for env-only configs
      GRAFOMEM_CGR_FORBIDDEN_TENANTS  comma-separated tenant_ids to always refuse
    """

    def __init__(self) -> None:
        self.base_url = os.environ.get("GRAFOMEM_API", "https://api.grafomem.com").rstrip("/")
        self.tenant_key = os.environ.get("GRAFOMEM_CGR_TENANT_KEY", "")
        # GRAFOMEM_CGR_DOGFOOD_TENANT is the pre-1.0 name, still honoured.
        self.expected_tenant = (os.environ.get("GRAFOMEM_CGR_TENANT")
                                or os.environ.get("GRAFOMEM_CGR_DOGFOOD_TENANT", ""))
        self.forbidden_tenants = {
            t.strip() for t in
            os.environ.get("GRAFOMEM_CGR_FORBIDDEN_TENANTS", "").split(",") if t.strip()
        }
        # Role keys: a file path, or the same JSON inline (so a config can be env-only).
        inline = os.environ.get("GRAFOMEM_CGR_ROLE_KEYS_JSON", "").strip()
        role_keys_path = os.environ.get("GRAFOMEM_CGR_ROLE_KEYS", "")
        self.role_keys: dict[str, str] = {}
        if inline:
            self.role_keys = json.loads(inline)
        elif role_keys_path:
            with open(role_keys_path) as f:
                self.role_keys = json.load(f)

    def validate(self) -> None:
        """Fail closed on misconfiguration."""
        missing = [n for n, v in (
            ("GRAFOMEM_CGR_TENANT_KEY", self.tenant_key),
            ("GRAFOMEM_CGR_TENANT", self.expected_tenant),
        ) if not v]
        if missing:
            raise SystemExit(f"cgr-capture: missing required config: {', '.join(missing)}")
        _assert_not_forbidden(self.expected_tenant, self.forbidden_tenants,
                              where="the configured GRAFOMEM_CGR_TENANT")
        if not self.role_keys:
            raise SystemExit(
                "cgr-capture: no role keys — set GRAFOMEM_CGR_ROLE_KEYS to a JSON file "
                "(or GRAFOMEM_CGR_ROLE_KEYS_JSON to the JSON itself) mapping role handles "
                'to agent_key hex, e.g. {"cc-builder@acme": "<hex>"}.')


# ============================================================================
# The capture client — talks to the governed HTTP API with your tenant key.
# ============================================================================

class CaptureClient:
    def __init__(self, cfg: Config, *, timeout: float = 30.0) -> None:
        self._cfg = cfg
        self._timeout = timeout

    def _api(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self._cfg.base_url + path, data=data, method=method,
            headers={"X-API-Key": self._cfg.tenant_key, "User-Agent": _UA,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status, json.loads(resp.read() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, None

    # ── runtime tenant guard (invariant 1): verify the API key's REAL tenant ──
    def _guard_response_tenant(self, resp: object) -> None:
        tid = resp.get("decision_record", {}).get("tenant_id") if isinstance(resp, dict) else None
        tid = tid or (resp.get("tenant_id") if isinstance(resp, dict) else None)
        if tid is None:
            return  # nothing to check on this response shape
        _assert_not_forbidden(tid, self._cfg.forbidden_tenants,
                              where="the API key's resolved tenant")
        if self._cfg.expected_tenant and tid != self._cfg.expected_tenant:
            raise SystemExit(
                f"REFUSING: the API key resolved to tenant {tid!r}, not the configured "
                f"GRAFOMEM_CGR_TENANT {self._cfg.expected_tenant!r}. Refusing to capture "
                "to an unexpected tenant.")

    # ── decision ──────────────────────────────────────────────────────────────
    def record_decision(self, *, work_item_id: str, agent_handle: str, domain: str,
                        decision: str, verifiability_tag: str = "judgment",
                        reason_code: str | None = None, reason_text: str = "",
                        # accepted for forward compat; NOT persisted today (see README)
                        agent_confidence: float | None = None,
                        agent_tier: float | None = None) -> dict:
        if domain not in DEV_DOMAINS:
            raise ValueError(f"unknown domain {domain!r}; expected one of {DEV_DOMAINS}")
        if decision not in ("certify", "reject"):
            raise ValueError(f"decision must be certify|reject, got {decision!r}")
        if verifiability_tag not in ("judgment", "rule"):
            raise ValueError(f"verifiability_tag must be judgment|rule, got {verifiability_tag!r}")
        agent_key = self._cfg.role_keys.get(agent_handle)
        if not agent_key:
            raise ValueError(
                f"no configured role key for {agent_handle!r}; known roles: "
                f"{sorted(self._cfg.role_keys)}")

        # `domain` is sent as the dedicated field → stored DURABLY server-side in the
        # never-encrypted, CGR-readable `parameters` as `cgr_domain` (single source of
        # truth), so per-domain re-scoring can attribute it later. v1 scoring is
        # single-dimension and does not read it yet ("capture now, score later"). The
        # context/reason carry only a human-readable trail (NOT the durable record).
        context = {"work_item_id": work_item_id, "reason_code": reason_code,
                   "captured_at": _now_iso()}
        reason = f"[{domain}] {reason_text}".strip()
        payload = {
            "decision": decision, "reason": reason, "invoice_id": work_item_id,
            "context": context, "agent_handle": agent_handle, "agent_key": agent_key,
            "verifiability_tag": verifiability_tag, "agent_tier": agent_tier,
            "domain": domain,   # durable → parameters.cgr_domain
        }
        code, body = self._api("POST", "/v1/governed/decisions", payload)
        if code != 200:
            raise RuntimeError(f"record_decision failed: HTTP {code}: {body}")
        self._guard_response_tenant(body)
        dr = body.get("decision_record", {}) if isinstance(body, dict) else {}
        return {"recorded": True, "decision_id": dr.get("decision_id"),
                "agent_handle": agent_handle, "domain": domain, "decision": decision,
                "verifiability_tag": verifiability_tag, "work_item_id": work_item_id}

    # ── outcome (binary mapping, never falsely resolve) ─────────────────────────
    def record_outcome(self, *, work_item_id: str, result: str,
                       source: str = "grafomem-cgr-mcp") -> dict:
        outcome = map_dev_outcome(result)
        if outcome is None:
            return {"posted": False, "result": result,
                    "reason": "no scored CGR mapping — decision left pending (invariant 4)"}
        code, body = self._api("POST", "/v1/governed/outcomes",
                               {"invoice_ref": work_item_id, "outcome": outcome, "source": source})
        if code != 200:
            raise RuntimeError(f"record_outcome failed: HTTP {code}: {body}")
        return {"posted": True, "result": result, "outcome": outcome,
                "work_item_id": work_item_id, "response": body}

    # ── durability guard: prove the DEPLOYED API actually persisted cgr_domain ──────
    def verify_domain_durable(self, decision_id: str, expected_domain: str) -> dict:
        """Confirm the just-recorded decision came back from the server with cgr_domain ==
        expected. Guards the silent-drop class: if the deployed API predates the
        domain-durability change, the unknown `domain` field is dropped and the decision
        lands WITHOUT cgr_domain — quietly defeating durability. This makes that loud.

        Reads /v1/cgr/substrate/export (needs decisions:read) and matches on decision_id."""
        code, body = self._api("GET", "/v1/cgr/substrate/export")
        if code != 200 or not isinstance(body, dict):
            raise SystemExit(
                f"cgr-capture: DURABILITY GUARD could not run — /v1/cgr/substrate/export "
                f"returned HTTP {code}. Ensure your API key has decisions:read.")
        rows = body.get("decisions") or []
        row = next((r for r in rows if r.get("decision_id") == decision_id), None)
        if row is None:
            raise SystemExit(
                f"cgr-capture: DURABILITY GUARD — decision {decision_id} not found in the "
                "substrate export; cannot confirm cgr_domain persisted.")
        got = row.get("cgr_domain")
        if got != expected_domain:
            raise SystemExit(
                "cgr-capture: DURABILITY GUARD FAILED — the deployed API did NOT echo "
                f"cgr_domain (expected {expected_domain!r}, got {got!r}): it silently "
                "dropped the `domain` field. Do NOT capture until the API supports domain "
                "durability, or the domain is lost — exactly the silent drop this guard "
                "exists to prevent.")
        return {"durable": True, "decision_id": decision_id, "cgr_domain": got}

    # ── reads (for selftest / acceptance only) ──────────────────────────────────
    def get_score(self, agent_handle: str) -> tuple[int, object]:
        return self._api("GET", f"/v1/cgr/scores/{agent_handle}")

    def usage(self) -> object:
        code, body = self._api("GET", "/v1/usage/current")
        return body if code == 200 else {"usage_read": f"unavailable (HTTP {code})"}


# ============================================================================
# MCP server — two tools over stdio.
# ============================================================================

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "work_item_id": {"type": "string", "description": "stable join key (PR#, task id, run id)"},
        "agent_handle": {"type": "string", "description": "role identity, e.g. cc-builder@acme",
                         "enum": None},  # filled from config at build time
        "domain": {"type": "string", "enum": list(DEV_DOMAINS)},
        "decision": {"type": "string", "enum": ["certify", "reject"]},
        "verifiability_tag": {"type": "string", "enum": ["judgment", "rule"], "default": "judgment"},
        "reason_code": {"type": "string"},
        "reason_text": {"type": "string"},
        "agent_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["work_item_id", "agent_handle", "domain", "decision"],
}

_OUTCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "work_item_id": {"type": "string", "description": "same join key used at decision time"},
        "result": {"type": "string",
                   "description": "dev result label, e.g. deploy_succeeded / vuln_found / "
                                  "review_confirmed; unmapped labels leave the decision pending"},
        "source": {"type": "string", "default": "grafomem-cgr-mcp"},
    },
    "required": ["work_item_id", "result"],
}


def build_mcp_server(cfg: Config):
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    client = CaptureClient(cfg)
    # Advertise our own version, not the MCP SDK's (the SDK default).
    server = Server("grafomem-cgr", version=__version__,
                    website_url="https://grafomem.com")

    decision_schema = json.loads(json.dumps(_DECISION_SCHEMA))
    decision_schema["properties"]["agent_handle"]["enum"] = sorted(cfg.role_keys)

    @server.list_tools()
    async def list_tools() -> list["Tool"]:
        return [
            Tool(name="cgr_record_decision",
                 description="Record a governed dev-loop JUDGMENT (certify/reject) attributed to a "
                             "role identity, in a domain, keyed by a stable work_item_id. Capture "
                             "now, score later. Only judgment+certify moves the score.",
                 inputSchema=decision_schema),
            Tool(name="cgr_record_outcome",
                 description="Record the resolved OUTCOME for a work_item_id. Maps the result to "
                             "success/failure; an unmapped result is a no-op (decision left pending, "
                             "never falsely resolved).",
                 inputSchema=json.loads(json.dumps(_OUTCOME_SCHEMA))),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list["TextContent"]:
        try:
            if name == "cgr_record_decision":
                res = client.record_decision(**arguments)
            elif name == "cgr_record_outcome":
                res = client.record_outcome(**arguments)
            else:
                res = {"error": f"unknown tool {name!r}"}
        except (Exception, SystemExit) as e:
            # surface ALL errors as tool output — including a guard's SystemExit (forbidden
            # tenant / tenant mismatch) — so a refused call returns gracefully and never
            # crashes the session's MCP server. (Does not catch CancelledError/KeyboardInterrupt.)
            res = {"error": f"{type(e).__name__}: {e}"}
        return [TextContent(type="text", text=json.dumps(res, default=str))]

    server._test_call_tool = call_tool  # test hook
    return server


async def _run_stdio(cfg: Config) -> None:
    from mcp.server.stdio import stdio_server
    server = build_mcp_server(cfg)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# ============================================================================
# CLI — serve (default), setup (register roles), selftest (full-loop acceptance).
# ============================================================================

def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_serve(cfg: Config) -> None:
    cfg.validate()
    # invariant 3: surface the meter so billing is conscious, not accidental.
    print(f"[cgr-capture] serving. tenant={cfg.expected_tenant} roles={sorted(cfg.role_keys)}",
          file=sys.stderr)
    print(f"[cgr-capture] NOTE: governed decisions meter as usage on this tenant's plan "
          f"(~handful/day expected). current usage: {json.dumps(CaptureClient(cfg).usage())}",
          file=sys.stderr)
    asyncio.run(_run_stdio(cfg))


def cmd_setup(cfg: Config) -> None:
    """Register the role identities on your tenant (idempotent, optional — the
    /v1/governed/decisions path attributes by handle+key without orchestrator
    registration, but registering keeps the roster explicit)."""
    cfg.validate()
    client = CaptureClient(cfg)
    out = {"tenant": cfg.expected_tenant, "roles": []}
    for handle, key in sorted(cfg.role_keys.items()):
        code, body = client._api("POST", "/v1/orchestrator/agents", {
            "name": handle, "role": "executor", "model_id": "claude-opus-4-8",
            "system_prompt": "", "tools": [], "agent_key": key, "agent_handle": handle,
        })
        out["roles"].append({"handle": handle, "status": code,
                             "agent_id": body.get("agent_id") if isinstance(body, dict) else None})
    _print(out)


def cmd_selftest(cfg: Config, *, handle: str, domain: str, work_item_id: str) -> None:
    """Close the full loop ONCE: record a judgment → prove the domain persisted →
    record its outcome → read the score, printing the movement + meter."""
    cfg.validate()
    client = CaptureClient(cfg)
    result: dict = {"tenant": cfg.expected_tenant, "handle": handle, "domain": domain,
                    "work_item_id": work_item_id, "usage_before": client.usage()}

    _, before = client.get_score(handle)
    result["score_before"] = before

    result["decision"] = client.record_decision(
        work_item_id=work_item_id, agent_handle=handle, domain=domain,
        decision="certify", verifiability_tag="judgment",
        reason_code="clean", reason_text="selftest judgment")

    # DURABILITY GUARD (runs BEFORE the outcome/score claim): prove the deployed API
    # actually persisted cgr_domain. If it didn't, this aborts loudly rather than
    # quietly recording a domainless decision.
    result["durability"] = client.verify_domain_durable(
        result["decision"]["decision_id"], domain)

    result["outcome"] = client.record_outcome(work_item_id=work_item_id, result="deploy_succeeded")

    _, after = client.get_score(handle)
    result["score_after"] = after
    result["usage_after"] = client.usage()
    _print(result)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="grafomem-cgr",
        description="grafomem-cgr — capture MCP server for GRAFOMEM Cloud CGR substrate.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"grafomem-cgr {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the stdio MCP server (the default with no arguments)")
    sub.add_parser("setup", help="register the role identities on your tenant (idempotent)")
    st = sub.add_parser("selftest", help="full-loop acceptance: decision → outcome → score")
    st.add_argument("--handle", required=True)
    st.add_argument("--domain", default="deploy-verification", choices=DEV_DOMAINS)
    st.add_argument("--work-item", default=None)

    args = ap.parse_args(argv)
    cfg = Config()
    # Bare `grafomem-cgr` serves — so an MCP client config is just the command + env.
    if args.cmd in (None, "serve"):
        cmd_serve(cfg)
    elif args.cmd == "setup":
        cmd_setup(cfg)
    elif args.cmd == "selftest":
        work_item = args.work_item or f"selftest-{int(datetime.now(timezone.utc).timestamp())}"
        cmd_selftest(cfg, handle=args.handle, domain=args.domain, work_item_id=work_item)


if __name__ == "__main__":
    main()
