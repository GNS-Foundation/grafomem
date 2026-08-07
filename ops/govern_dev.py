#!/usr/bin/env python3
"""Governed dev loop v0 — "Grafomem builds Grafomem".

A thin bridge that governs the ENGINEERING decision inside Grafomem, using the SAME
governed substrate that governs GTM outreach: propose → policy gate → (HITL for a
consequential action) → the operator executes through the git pipeline → outcome → CGR.
It is the general "governed operating agent" template, instantiated first for engineering.

HARD INVARIANTS (Cowork Step-0 review conditions — see docs/cgr/track2-governed-dev-loop-v0.md):

  (1) propose ≠ attest.  This bridge PROPOSES and RECORDS OUTCOMES only. It holds NO
      approver key and exposes NO attest/approve/sign method — it CANNOT self-approve a
      consequential action. Approval is a DISTINCT Ed25519 signer (a human), out of band,
      through the HITL attest endpoint. The absence of an approver key here IS the
      invariant; tests/test_governed_dev_loop.py proves a bridge-held key can't attest.

  (2) Test tenant, never corp.  The loop writes dev-outcome CGR data; running it on corp
      would pollute corp's GTM substrate. setup_dev_loop() REFUSES the corp tenant.

  (3) invoice_ref-as-work-item-id + receivables-vocab reuse is the accepted v0 shortcut.
      The overload (one field/vocab serving two domains) is logged as the Phase-2
      "generalize substrate schema" ROADMAP item in the design note — NOT fixed here.

  (4) Binary outcome mapping.  compute_scores (aml.cgr.scoring.score_agent) scores outcomes
      BINARY at full weight: 'paid' → α (success), 'default' → β (failure); EVERY other
      label (disputed/late/written_off/None) is unscored/pending. map_dev_outcome() makes
      the positive/negative assignment explicit and returns None for anything ambiguous, so
      an in-flight or unclear result is left PENDING rather than falsely resolved.

The bridge talks to Grafomem over the HTTP API with a tenant token — NEVER raw DB. The loop
COMPOSES with the git pipeline; it does not bypass it. "Approved in Grafomem" never merges
past a red CI or skips Cowork review — CI + branch protection + review still enforce at merge.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# A browser-ish UA — the prod edge (Cloudflare) blocks default urllib UAs on these paths.
_UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

# The corp tenant — off-limits for the dev loop (condition 2).
CORP_TENANT = "5605470cfa8e415ba418c9d8944abf9a"

# ── the dev tool taxonomy (condition — policy scoped to the consequential set only) ──
CONSEQUENTIAL_TOOLS = ["deploy", "apply_migration", "merge_pr"]   # escalate → HITL, execute-on-approval
LOW_RISK_TOOLS = ["open_pr", "run_ci"]                            # allow + record, no HITL
DEV_TOOLS = CONSEQUENTIAL_TOOLS + LOW_RISK_TOOLS

# The HITL policy — mirrors the live GTM send_email policy shape (hitl_required + a `tools`
# filter under op=tool_execution). Escalates EXACTLY the consequential set; the low-risk tools
# are absent from `tools`, so the gate ALLOWS them (recorded as governed decisions, no HITL).
DEV_HITL_POLICY = {
    "name": "dev-loop HITL — consequential tools",
    "description": ("Escalate deploy/apply_migration/merge_pr to a human Ed25519 approver; "
                    "open_pr/run_ci allow+record. Mirrors the send_email HITL policy shape."),
    "policy_type": "hitl_required",
    "action": "escalate",
    "config": {"operations": ["tool_execution"], "tools": CONSEQUENTIAL_TOOLS},
    "priority": 50,
}

# ── condition 4: the eng → CGR-receivables-vocabulary mapping, made explicit ──────────
# compute_scores scores ONLY 'paid' (positive/α) and 'default' (negative/β), at full weight,
# and ONLY for decision=certify + verifiability_tag=judgment (which propose_action already
# tags). Every other outcome label is treated as UNRESOLVED (n_pending), moving no score.
# So a dev result must land on exactly {paid, default} to update CGR; ambiguous/in-flight
# results map to None → do NOT post (a mis-mapped label would falsely resolve the decision
# and corrupt CGR — the Phase-0 synthetic-data lesson).
DEV_OUTCOME_MAP = {
    # positive → 'paid' (the proposed change proved out)
    "deploy_succeeded": "paid",
    "migration_applied": "paid",
    "ci_passed": "paid",
    "merge_landed": "paid",       # merged AND not reverted within the observation window
    "pr_merged": "paid",
    # negative → 'default' (the proposed change failed / was undone)
    "deploy_failed": "default",
    "deploy_rolled_back": "default",
    "migration_failed": "default",
    "ci_failed": "default",
    "merge_reverted": "default",
}


def map_dev_outcome(dev_result: str) -> str | None:
    """Map an engineering result label onto the CGR receivables vocabulary.

    Returns 'paid' (success → α), 'default' (failure → β), or None when the result has no
    scored mapping (ambiguous, superseded, or still in flight). None means "do NOT post an
    outcome" — leaving the decision pending is correct; a falsely-resolved decision corrupts
    the score. See condition 4 in the module docstring."""
    return DEV_OUTCOME_MAP.get(dev_result)


# ============================================================================
# The bridge — propose → poll → record outcome, over the HTTP API only.
#
# Deliberately has NO attest()/approve()/sign() method and NO approver key (condition 1).
# ============================================================================

class GovernedDevBridge:
    """Governs a dev action through Grafomem's HTTP API with a tenant token.

    Lifecycle for a CONSEQUENTIAL action:
        propose(deploy, ...) → escalated=True, hitl_request_id set, executed=False
        poll_until_resolved(request_id)  → waits for a HUMAN to approve/deny out of band
                                           (this bridge never attests — it only reads status)
        [operator runs the real deploy through the git pipeline once approved]
        record_outcome(work_item_id, "deploy_succeeded")  → 'paid' → CGR

    Lifecycle for a LOW-RISK action (open_pr/run_ci):
        propose(open_pr, ...) → escalated=False, no HITL — recorded as a governed decision.
        [the real PR/CI runs in the git pipeline]
        record_outcome(...) when the result is known.
    """

    def __init__(self, base_url: str, api_key: str, *, agent_id: str,
                 user_agent: str = _UA, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._agent_id = agent_id
        self._ua = user_agent
        self._timeout = timeout

    def _api(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self._base + path, data=data, method=method,
            headers={"X-API-Key": self._key, "User-Agent": self._ua,
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

    # ── propose ──────────────────────────────────────────────────────────────
    def propose(self, tool: str, args: dict, work_item_id: str, reason: str = "") -> dict:
        """Propose a dev action. `work_item_id` (a PR#/task-id) is the invoice_ref join key
        (condition 3). Returns the propose_action response (decision_id, escalated,
        hitl_request_id, executed, ...). For a consequential tool the policy escalates and
        the action is NOT executed until a human approves."""
        status, body = self._api(
            "POST", f"/v1/orchestrator/agents/{self._agent_id}/propose",
            {"tool": tool, "args": args, "invoice_ref": work_item_id, "reason": reason},
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"propose failed: HTTP {status}: {body}")
        return body

    # ── poll (read-only — NEVER attests) ─────────────────────────────────────
    def list_hitl(self, status: str = "pending") -> list[dict]:
        code, body = self._api("GET", f"/v1/hitl/requests?status={status}")
        if code != 200 or not isinstance(body, dict):
            raise RuntimeError(f"list_hitl failed: HTTP {code}: {body}")
        return body.get("requests", [])

    def poll_until_resolved(self, request_id: str, *, interval: float = 5.0,
                            max_wait: float = 3600.0) -> str:
        """Poll HITL status until the request leaves 'pending' (a human approved/denied it)
        or the wall-clock budget elapses. READ-ONLY — the bridge cannot and does not attest.
        Returns the terminal status ('approved'|'denied'|'pending' on timeout)."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            for r in self.list_hitl(status="approved"):
                if r.get("request_id") == request_id:
                    return "approved"
            for r in self.list_hitl(status="denied"):
                if r.get("request_id") == request_id:
                    return "denied"
            time.sleep(interval)
        return "pending"

    # ── outcome (binary mapping) ─────────────────────────────────────────────
    def record_outcome(self, work_item_id: str, dev_result: str, *,
                        amount: float | None = None, source: str = "dev-loop") -> dict:
        """Record the terminal engineering outcome for a work item. Maps `dev_result` onto
        {paid, default} via map_dev_outcome; a None mapping is a NO-OP (left pending)."""
        outcome = map_dev_outcome(dev_result)
        if outcome is None:
            return {"posted": False, "dev_result": dev_result,
                    "reason": "no scored CGR mapping — decision left pending (condition 4)"}
        payload = {"invoice_ref": work_item_id, "outcome": outcome, "source": source}
        if amount is not None:
            payload["amount_recovered"] = amount
        code, body = self._api("POST", "/v1/governed/outcomes", payload)
        if code != 200:
            raise RuntimeError(f"record_outcome failed: HTTP {code}: {body}")
        return {"posted": True, "dev_result": dev_result, "outcome": outcome, "response": body}


# ============================================================================
# Idempotent setup — register the eng-agent + tool stubs + HITL policy via the API.
# ============================================================================

def _admin_api(base_url: str, api_key: str, method: str, path: str,
               body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data=data, method=method,
        headers={"X-API-Key": api_key, "User-Agent": _UA, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


def setup_dev_loop(base_url: str, api_key: str, tenant_id: str, *,
                   agent_key: str, agent_handle: str = "eng-agent@ulissy",
                   force_corp: bool = False) -> dict:
    """Register the eng-agent, the 5 dev tool stubs, and the consequential-tools HITL policy
    on `tenant_id`. Idempotent. Uses the API with an admin token — never raw DB.

    REFUSES the corp tenant (condition 2) unless force_corp is explicitly set (it never
    should be for the dev loop)."""
    if tenant_id == CORP_TENANT and not force_corp:
        raise SystemExit(
            "REFUSING to set up the governed dev loop on the corp tenant (Cowork condition 2): "
            "the loop writes dev-outcome CGR data and must not pollute corp's GTM substrate. "
            "Use a dedicated test/eng tenant.")

    out: dict = {"tenant_id": tenant_id, "agent_handle": agent_handle}

    # 1. eng-agent — the CGR-attributed subject. agent_key makes its decisions scorable.
    code, body = _admin_api(base_url, api_key, "POST", "/v1/orchestrator/agents", {
        "name": agent_handle, "role": "executor",
        "model_id": "claude-opus-4-8", "system_prompt": "",
        "tools": DEV_TOOLS, "agent_key": agent_key, "agent_handle": agent_handle,
    })
    out["agent"] = {"status": code, "agent_id": body.get("agent_id") if isinstance(body, dict) else None}

    # 2. dev tool stubs — CUSTOM type, no webhook_url ⇒ a harmless no-op executor. These are
    #    MARKERS: the real work (the deploy, the merge) stays in the git pipeline; the stub's
    #    "execution" on approval is a no-op, so the loop governs the DECISION, not the doing.
    out["tools"] = []
    for t in DEV_TOOLS:
        code, _ = _admin_api(base_url, api_key, "POST", "/v1/llm/tools", {
            "name": t, "description": f"dev-loop stub: {t} (approved_to_execute marker; real work in git pipeline)",
            "tool_type": "custom", "input_schema": {"type": "object"},
            "config": {}, "requires_governance": True,
        })
        out["tools"].append({"name": t, "status": code})

    # 3. the HITL policy scoped to the consequential set only.
    code, body = _admin_api(base_url, api_key, "POST", "/v1/governance/policies", DEV_HITL_POLICY)
    out["policy"] = {"status": code, "policy_id": body.get("policy_id") if isinstance(body, dict) else None}
    return out


def _load_key(path: str, field: str = "api_key") -> str:
    with open(path) as f:
        return json.load(f)[field]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="register eng-agent + tool stubs + HITL policy (test tenant)")
    s.add_argument("--base-url", default=os.environ.get("GRAFOMEM_API", "https://api.grafomem.com"))
    s.add_argument("--creds", required=True, help="JSON file with an admin api_key for the TEST tenant")
    s.add_argument("--tenant", required=True, help="the TEST tenant_id (corp is refused)")
    s.add_argument("--agent-key", required=True, help="the eng-agent's stable CGR agent_key (hex)")

    p = sub.add_parser("propose", help="propose a dev action")
    p.add_argument("--base-url", default=os.environ.get("GRAFOMEM_API", "https://api.grafomem.com"))
    p.add_argument("--creds", required=True)
    p.add_argument("--agent-id", required=True)
    p.add_argument("--tool", required=True, choices=DEV_TOOLS)
    p.add_argument("--work-item", required=True, help="PR#/task-id (the invoice_ref join key)")
    p.add_argument("--reason", default="")

    args = ap.parse_args(argv)
    if args.cmd == "setup":
        print(json.dumps(setup_dev_loop(args.base_url, _load_key(args.creds), args.tenant,
                                        agent_key=args.agent_key), indent=2))
    elif args.cmd == "propose":
        bridge = GovernedDevBridge(args.base_url, _load_key(args.creds), agent_id=args.agent_id)
        print(json.dumps(bridge.propose(args.tool, {}, args.work_item, args.reason), indent=2))


if __name__ == "__main__":
    main()
