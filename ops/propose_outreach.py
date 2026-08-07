"""Phase 2 — re-home the GTM outreach proposals onto the corp tenant THROUGH the orchestrator.

For each ledger row, POST /v1/orchestrator/agents/{agent_id}/propose (PR-0) so the proposal is
recorded as a CGR-attributed governed decision carrying the agent's stable agent_key +
invoice_ref. The agent then shows an (unproven/pending) CGR score in Reputation.

GUARDRAIL: this only PROPOSES (records decisions). It never sends — no send path here.

Idempotent: a local manifest (.ulissy_propose_state.json) of proposed refs prevents re-runs
from double-counting (propose has no server-side dedup, same as /v1/governed/decisions).

    python ops/propose_outreach.py            # uses ops/ledger.csv if present, else sample
    python ops/propose_outreach.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_front as ing  # noqa: E402  (load_rows / make_ref / helpers)
from common import BASE, client, load_creds  # noqa: E402

AGENT_NAME = "gtm-outreach-agent@ulissy"


def _ledger_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    real = os.path.join(here, "ledger.csv")
    return real if os.path.exists(real) else os.path.join(here, "sample_ledger.csv")


def _state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ulissy_propose_state.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    creds = load_creds()
    key = creds["api_key"]
    path = _ledger_path()
    rows = ing.load_rows(path)
    print(f"BASE = {BASE}")
    print(f"tenant = {creds['tenant_id']} ({creds.get('email')})  ledger = {os.path.basename(path)} ({len(rows)} rows)")

    with client(key, timeout=90.0) as c:
        r = c.get("/v1/orchestrator/agents"); r.raise_for_status()
        agent = next((a for a in r.json().get("agents", []) if a.get("name") == AGENT_NAME), None)
        if not agent:
            sys.exit(f"agent {AGENT_NAME} not found — run ops/increment1.py first")
        agent_id = agent["agent_id"]

        state = ing.IngestState(_state_path())
        todo = [row for row in rows if not state.has(ing.make_ref(row))]
        print(f"agent_id = {agent_id}  |  to propose: {len(todo)}  (skipping {len(rows) - len(todo)} already proposed)")

        if args.dry_run:
            for row in todo[:2]:
                ref = ing.make_ref(row)
                print(f"  would propose {ref}: send_email to {row.get('person') or row.get('company')}")
            return 0

        proposed = 0
        for row in todo:
            ref = ing.make_ref(row)
            body = {
                "tool": "send_email",
                "args": {
                    "to": str(row.get("person") or row.get("company") or ""),
                    "company": str(row.get("company", "") or ""),
                    "person": str(row.get("person", "") or ""),
                    "channel": str(row.get("channel", "") or ""),
                    "message_variant": str(row.get("message_variant", row.get("variant", "")) or ""),
                },
                "invoice_ref": ref,
                "reason": str(row.get("rationale", row.get("reason", "")) or "outreach proposed"),
            }
            rr = c.post(f"/v1/orchestrator/agents/{agent_id}/propose", json=body)
            rr.raise_for_status()
            state.add(ref); proposed += 1
        state.save()
        print(f"✅ proposed {proposed} outreach decisions through the orchestrator")

        sc = c.get("/v1/cgr/scores"); sc.raise_for_status()
        rowset = [s for s in sc.json().get("scores", []) if s.get("agent_handle") == AGENT_NAME]
        print("\n=== Reputation (corp tenant) ===")
        for s in rowset or [{}]:
            print(f"  {s.get('agent_handle','—')}: cgr_score={s.get('cgr_score')} tier={s.get('capability_tier')} "
                  f"n_resolved={s.get('n_resolved')} n_pending={s.get('n_pending')} "
                  f"alpha/beta={s.get('post_alpha')}/{s.get('post_beta')} conf={s.get('confidence')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
