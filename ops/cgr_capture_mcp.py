#!/usr/bin/env python3
"""grafomem-cgr capture MCP server — dogfood substrate (Track C, ticket 1).

Lets GRAFOMEM's own Claude agents (Claude Code / Cowork sessions) accumulate CGR
substrate from their dev-loop judgments: **capture now, score later.** A thin MCP
server exposing two tools that wrap the EXISTING governed HTTP path — no new data
plane, no scoring change, no new crypto.

  cgr_record_decision(work_item_id, agent_handle, domain, decision, verifiability_tag, ...)
        → POST /v1/governed/decisions   (signed decision_record + chained receipt)
  cgr_record_outcome(work_item_id, result, source?)
        → POST /v1/governed/outcomes    (append-only GMP Fact, joined by work_item_id)

Design refs: claude/track-c-ticket-1-grafomem-cgr-capture-mcp.md and
docs/cgr/cgr-substrate-instrumentation-spec.md (the "capture now, score later" rule).
Wraps the same path exercised by ops/govern_dev.py.

HARD INVARIANTS (mirror govern_dev.py + ticket-1 notes):

  (1) Dogfood/test tenant, NEVER corp.  The configured tenant MUST be the dogfood
      tenant and MUST NOT be corp. Enforced AS CODE at startup (config guard) and at
      runtime (every decision response's tenant_id is checked against corp — a corp
      response is a hard error). See _assert_not_corp / _guard_response_tenant.

  (2) Key custody.  The config holds ONLY the dogfood role identities (public agent_key
      per role handle) and the dogfood tenant credential. The tool caller picks a role
      HANDLE; the server injects that role's agent_key — a caller can never supply an
      arbitrary key or point at another tenant. (v0 capture uses the public agent_key as
      the CGR binding subject; no private key is used — per-decision proof-of-possession
      is later hardening, OQ-4.)

  (3) Billing is conscious, not accidental.  Governed decisions METER as real usage on
      the dogfood tenant's plan. Startup and selftest print the current governed-decision
      count (GET /v1/usage/current) so the meter is never a surprise. Expected volume is a
      handful/day — trivially inside any allotment.

  (4) Capture now, score later + never falsely resolve.  The irreversible fields
      (work_item_id join key, role handle, verifiability_tag, domain) are logged at
      decision time. An outcome result with no scored mapping is a NO-OP (decision left
      PENDING) — a falsely-resolved decision corrupts the score.

⚠ v0 SCOPE — single-dimension SCORING, but domain is stored DURABLY (build-step-0 finding):
  CGR *scoring* is single-dimension today (all judgment-certify decisions score under one
  dimension, reported as "receivables"). Per-domain *scoring* is the Phase-2 "generalize
  substrate schema" work (it touches the served scoring surface, out of this ops-only
  ticket). BUT the domain string IS captured durably right now: `cgr_record_decision` sends
  `domain` as a dedicated field and grafomem stores it server-side, per decision, in the
  never-encrypted, CGR-readable decision `parameters` as `cgr_domain` (surfaced by the
  substrate loader as `DecisionRow.cgr_domain` and in `/v1/cgr/substrate/export`). So
  per-domain re-scoring later is TRUE, not a hope — the domain lives in the signed decision
  record, not in any client-side log. (This required a small, additive, backward-compatible
  server change to the governed-decision write — see the PR; it is not launch-visible and
  merges post-launch.)
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

# A browser-ish UA — the prod edge (Cloudflare) blocks default urllib UAs on these paths.
_UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

# The corp tenant — off-limits for the dogfood loop (invariant 1). Mirrors govern_dev.py.
CORP_TENANT = "5605470cfa8e415ba418c9d8944abf9a"

# Locked dev-loop domain taxonomy (OQ-9). Captured as metadata in v0 (see module note).
DEV_DOMAINS = ("deploy-verification", "security-scan", "adversarial-review")

# Outcome result → CGR receivables-vocab (v0 reuse, condition 3). compute_scores scores ONLY
# 'paid' (α/success) and 'default' (β/failure) at full weight, and ONLY for
# decision=certify + verifiability_tag=judgment. Anything unmapped ⇒ NO-OP ⇒ left pending.
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


# ============================================================================
# Config — env-driven. Holds ONLY dogfood role public keys + dogfood tenant creds.
# ============================================================================

class Config:
    """Capture config loaded from the environment (invariant 2).

    Env:
      GRAFOMEM_API                base URL (default https://api.grafomem.com)
      GRAFOMEM_CGR_TENANT_KEY     the DOGFOOD tenant's X-API-Key (the sensitive secret)
      GRAFOMEM_CGR_DOGFOOD_TENANT the expected dogfood tenant_id (never-corp guard)
      GRAFOMEM_CGR_ROLE_KEYS      path to JSON { "<role@ulissy>": "<agent_key hex>", ... }
    """

    def __init__(self) -> None:
        self.base_url = os.environ.get("GRAFOMEM_API", "https://api.grafomem.com").rstrip("/")
        self.tenant_key = os.environ.get("GRAFOMEM_CGR_TENANT_KEY", "")
        self.dogfood_tenant = os.environ.get("GRAFOMEM_CGR_DOGFOOD_TENANT", "")
        role_keys_path = os.environ.get("GRAFOMEM_CGR_ROLE_KEYS", "")
        self.role_keys: dict[str, str] = {}
        if role_keys_path:
            with open(role_keys_path) as f:
                self.role_keys = json.load(f)

    def validate(self) -> None:
        """Fail closed on misconfiguration. The never-corp guard is CODE, not a comment."""
        missing = [n for n, v in (
            ("GRAFOMEM_CGR_TENANT_KEY", self.tenant_key),
            ("GRAFOMEM_CGR_DOGFOOD_TENANT", self.dogfood_tenant),
        ) if not v]
        if missing:
            raise SystemExit(f"cgr-capture: missing required config: {', '.join(missing)}")
        _assert_not_corp(self.dogfood_tenant, where="configured GRAFOMEM_CGR_DOGFOOD_TENANT")
        if not self.role_keys:
            raise SystemExit(
                "cgr-capture: no role keys — set GRAFOMEM_CGR_ROLE_KEYS to a JSON file "
                'mapping role handles to agent_key hex, e.g. {"cc-builder@ulissy": "<hex>"}.')


def _assert_not_corp(tenant_id: str, *, where: str) -> None:
    if tenant_id == CORP_TENANT:
        raise SystemExit(
            f"REFUSING: {where} is the CORP tenant. The dogfood capture loop must never "
            "write to corp (it would pollute corp's GTM substrate). Point it at the "
            "dogfood/Ulissy tenant.")


# ============================================================================
# The capture client — talks to the governed HTTP API with the dogfood tenant key.
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

    # ── runtime never-corp guard (invariant 1): verify the API key's REAL tenant ──
    def _guard_response_tenant(self, resp: object) -> None:
        tid = resp.get("decision_record", {}).get("tenant_id") if isinstance(resp, dict) else None
        tid = tid or (resp.get("tenant_id") if isinstance(resp, dict) else None)
        if tid is None:
            return  # nothing to check on this response shape
        _assert_not_corp(tid, where="the API key's resolved tenant")
        if self._cfg.dogfood_tenant and tid != self._cfg.dogfood_tenant:
            raise SystemExit(
                f"REFUSING: the API key resolved to tenant {tid!r}, not the configured "
                f"dogfood tenant {self._cfg.dogfood_tenant!r}. Refusing to capture to an "
                "unexpected tenant.")

    # ── decision ──────────────────────────────────────────────────────────────
    def record_decision(self, *, work_item_id: str, agent_handle: str, domain: str,
                        decision: str, verifiability_tag: str = "judgment",
                        reason_code: str | None = None, reason_text: str = "",
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
        # context/reason carry only a human-readable trail (NOT the durable domain record).
        context = {"work_item_id": work_item_id, "reason_code": reason_code,
                   "captured_at": _now_iso()}
        reason = f"[{domain}] {reason_text}".strip()
        payload = {
            "decision": decision, "reason": reason, "invoice_id": work_item_id,
            "context": context, "agent_handle": agent_handle, "agent_key": agent_key,
            "verifiability_tag": verifiability_tag, "agent_tier": agent_tier,
            "domain": domain,   # durable → parameters.cgr_domain (the field per-domain scoring reads)
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
                       source: str = "dogfood-mcp") -> dict:
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
        domain-durability change, Pydantic drops the unknown `domain` field and the decision
        lands WITHOUT cgr_domain — quietly defeating durability. This makes that loud.

        Reads /v1/cgr/substrate/export (needs decisions:read) and matches on decision_id."""
        code, body = self._api("GET", "/v1/cgr/substrate/export")
        if code != 200 or not isinstance(body, dict):
            raise SystemExit(
                f"cgr-capture: DURABILITY GUARD could not run — /v1/cgr/substrate/export "
                f"returned HTTP {code}. Ensure the dogfood key has decisions:read and that "
                "the domain-durability change (PR #67) is merged + DEPLOYED before capturing.")
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
                f"cgr_domain (expected {expected_domain!r}, got {got!r}). The deployed API "
                "predates the domain-durability change (PR #67): it silently dropped the "
                "unknown `domain` field. Do NOT capture until #67 is merged + deployed, or "
                "the domain is lost — exactly the silent drop this guard exists to prevent.")
        return {"durable": True, "decision_id": decision_id, "cgr_domain": got}

    # ── reads (for selftest / acceptance — NOT part of the public read surface, ticket 2) ──
    def get_score(self, agent_handle: str) -> tuple[int, object]:
        return self._api("GET", f"/v1/cgr/scores/{agent_handle}")

    def usage(self) -> object:
        code, body = self._api("GET", "/v1/usage/current")
        return body if code == 200 else {"usage_read": f"unavailable (HTTP {code})"}


# ============================================================================
# MCP server — two tools over stdio (mirrors src/aml/server/mcp.py:run_mcp_stdio).
# ============================================================================

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "work_item_id": {"type": "string", "description": "stable join key (PR#, task id, run id)"},
        "agent_handle": {"type": "string", "description": "role identity, e.g. cc-builder@ulissy",
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
        "source": {"type": "string", "default": "dogfood-mcp"},
    },
    "required": ["work_item_id", "result"],
}


def build_mcp_server(cfg: Config):
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    client = CaptureClient(cfg)
    server = Server("grafomem-cgr")

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
        except Exception as e:  # surface errors as tool output, don't crash the session
            res = {"error": f"{type(e).__name__}: {e}"}
        return [TextContent(type="text", text=json.dumps(res, default=str))]

    server._test_call_tool = call_tool  # test hook, mirrors aml/server/mcp.py
    return server


async def _run_stdio(cfg: Config) -> None:
    from mcp.server.stdio import stdio_server
    server = build_mcp_server(cfg)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# ============================================================================
# CLI — serve (stdio MCP), setup (register roles), selftest (full-loop acceptance).
# ============================================================================

def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_serve(cfg: Config) -> None:
    cfg.validate()
    # invariant 3: surface the meter so billing is conscious, not accidental.
    print(f"[cgr-capture] serving. tenant={cfg.dogfood_tenant} roles={sorted(cfg.role_keys)}",
          file=sys.stderr)
    print(f"[cgr-capture] NOTE: governed decisions meter as usage on this tenant's plan "
          f"(~handful/day expected). current usage: {json.dumps(CaptureClient(cfg).usage())}",
          file=sys.stderr)
    asyncio.run(_run_stdio(cfg))


def cmd_setup(cfg: Config) -> None:
    """Register the role identities on the dogfood tenant (idempotent, optional — the
    /v1/governed/decisions path attributes by handle+key without orchestrator registration,
    but registering keeps the roster explicit). Never corp."""
    cfg.validate()
    client = CaptureClient(cfg)
    out = {"tenant": cfg.dogfood_tenant, "roles": []}
    for handle, key in sorted(cfg.role_keys.items()):
        code, body = client._api("POST", "/v1/orchestrator/agents", {
            "name": handle, "role": "executor", "model_id": "claude-opus-4-8",
            "system_prompt": "", "tools": [], "agent_key": key, "agent_handle": handle,
        })
        out["roles"].append({"handle": handle, "status": code,
                             "agent_id": body.get("agent_id") if isinstance(body, dict) else None})
    _print(out)


def cmd_selftest(cfg: Config, *, handle: str, domain: str, work_item_id: str) -> None:
    """Close the full loop ONCE (acceptance / "first real CGR evidence" moment):
    record a judgment → record its outcome → read the score, printing the movement + meter."""
    cfg.validate()
    client = CaptureClient(cfg)
    result: dict = {"tenant": cfg.dogfood_tenant, "handle": handle, "domain": domain,
                    "work_item_id": work_item_id, "usage_before": client.usage()}

    _, before = client.get_score(handle)
    result["score_before"] = before

    result["decision"] = client.record_decision(
        work_item_id=work_item_id, agent_handle=handle, domain=domain,
        decision="certify", verifiability_tag="judgment",
        reason_code="clean", reason_text="selftest judgment")

    # DURABILITY GUARD (runs BEFORE the outcome/score claim): prove the deployed API
    # actually persisted cgr_domain. If it didn't (deploy behind the durability change),
    # this aborts loudly rather than quietly recording a domainless decision.
    result["durability"] = client.verify_domain_durable(
        result["decision"]["decision_id"], domain)

    result["outcome"] = client.record_outcome(work_item_id=work_item_id, result="deploy_succeeded")

    _, after = client.get_score(handle)
    result["score_after"] = after
    result["usage_after"] = client.usage()
    _print(result)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the stdio MCP server (connect to Claude Code / Cowork)")
    sub.add_parser("setup", help="register the dogfood role identities (idempotent, never corp)")
    st = sub.add_parser("selftest", help="full-loop acceptance: decision → outcome → score")
    st.add_argument("--handle", default="cc-builder@ulissy")
    st.add_argument("--domain", default="deploy-verification", choices=DEV_DOMAINS)
    st.add_argument("--work-item", default=f"selftest-{int(datetime.now(timezone.utc).timestamp())}")

    args = ap.parse_args(argv)
    cfg = Config()
    if args.cmd == "serve":
        cmd_serve(cfg)
    elif args.cmd == "setup":
        cmd_setup(cfg)
    elif args.cmd == "selftest":
        cmd_selftest(cfg, handle=args.handle, domain=args.domain, work_item_id=args.work_item)


if __name__ == "__main__":
    main()
