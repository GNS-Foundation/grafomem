"""Task 3 — deterministic verification agent (honest stand-in, no LLM).

Applies three plain rules per invoice; every decision (certify OR reject) is
submitted to GRAFOMEM as a governed decision, which returns a decision_record
and a signed execution_receipt.
"""
from __future__ import annotations

import json
import os

from common import BASE, client, load_creds

INVOICES = os.path.join(os.path.dirname(__file__), "demo_invoices.json")


def strip_annotations(inv: dict) -> dict:
    """Drop any helper/annotation key (starts with '_', e.g. _fraud). These are
    review notes only — they must never reach the decision logic or the payload."""
    return {k: v for k, v in inv.items() if not k.startswith("_")}


def load_invoices() -> list[dict]:
    """Load invoices from the {'invoices': [...]} file, annotation keys stripped.
    The agent decides from the ACTUAL fields only — never from a '_fraud' hint."""
    raw = json.load(open(INVOICES))
    items = raw["invoices"] if isinstance(raw, dict) else raw
    return [strip_annotations(i) for i in items]


def decide(inv: dict, certified_ids: set) -> tuple[str, str]:
    """Return (decision, reason). All rules must pass to certify."""
    if inv["invoice_amount"] > inv["po_amount"]:
        return "reject", "Invoice amount exceeds authorized PO"
    if inv.get("approval_status") != "approved":
        return "reject", "No verified approval from debtor"
    if inv["invoice_id"] in certified_ids:
        return "reject", "Duplicate of already-certified invoice"
    return "certify", "Amount within PO, approved, and not a duplicate"


def run_agent(api_key: str, invoices: list[dict], verbose: bool = True) -> list[dict]:
    certified_ids: set = set()
    results = []
    with client(api_key) as c:
        for raw in invoices:
            inv = strip_annotations(raw)          # never let a '_' key reach the payload
            decision, reason = decide(inv, certified_ids)
            r = c.post("/v1/governed/decisions", json={
                "decision": decision,
                "reason": reason,
                "invoice_id": inv["invoice_id"],
                "context": inv,
            })
            r.raise_for_status()
            body = r.json()
            # Mark certified ONLY after a successful certify decision is recorded.
            if decision == "certify":
                certified_ids.add(inv["invoice_id"])
            results.append({
                "invoice_id": inv["invoice_id"], "decision": decision, "reason": reason,
                "decision_id": body["decision_record"]["decision_id"],
                "receipt_id": body["execution_receipt"]["receipt_id"],
                "response": body,
            })
            if verbose:
                mark = "CERTIFY" if decision == "certify" else "REJECT "
                print(f"  [{mark}] {inv['invoice_id']:<16} {inv['vendor'][:30]:<30} -> {reason}")
    return results


def summary(results: list[dict]) -> None:
    certified = [r for r in results if r["decision"] == "certify"]
    rejected = [r for r in results if r["decision"] == "reject"]
    print(f"\nBatch summary: {len(certified)} certified, {len(rejected)} rejected")
    for r in rejected:
        print(f"  REJECTED {r['invoice_id']}: {r['reason']}")


if __name__ == "__main__":
    creds = load_creds()
    invoices = load_invoices()
    print(f"BASE = {BASE}  |  {len(invoices)} invoices  |  tenant {creds['tenant_id']}")
    results = run_agent(creds["api_key"], invoices)
    summary(results)
