"""Server-side invoice-verification rules engine.

Deterministic checks applied to each submitted invoice. The policy is
configurable per request, so a customer expresses their own field names,
thresholds, and which checks are active without a code change — the decision it
returns is then recorded as a signed, tamper-evident governed decision.

evaluate_invoice() returns (decision, reason) where decision is "certify" or
"reject". Duplicate detection is caller-scoped: pass the set of invoice_ids
already certified in this batch.
"""
from __future__ import annotations

from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    "require_amount_within_po": True,
    "require_approval": True,
    "reject_duplicates": True,
    # Field mapping — override to match a customer's invoice schema.
    "invoice_amount_field": "invoice_amount",
    "po_amount_field": "po_amount",
    "approval_field": "approval_status",
    "approved_value": "approved",
    "invoice_id_field": "invoice_id",
}


def resolve_policy(policy: dict | None) -> dict[str, Any]:
    return {**DEFAULT_POLICY, **(policy or {})}


def evaluate_invoice(inv: dict, policy: dict | None, certified_ids: set) -> tuple[str, str]:
    """Apply the (resolved) policy to one invoice. Returns (decision, reason)."""
    p = resolve_policy(policy)
    # Ignore any annotation/helper keys (e.g. '_fraud') — decide only from real fields.
    inv = {k: v for k, v in inv.items() if not str(k).startswith("_")}

    if p["require_amount_within_po"]:
        try:
            inv_amt = float(inv.get(p["invoice_amount_field"]))
            po_amt = float(inv.get(p["po_amount_field"]))
        except (TypeError, ValueError):
            return "reject", "Invoice or PO amount is missing or non-numeric"
        if inv_amt > po_amt:
            return "reject", "Invoice amount exceeds authorized PO"

    if p["require_approval"]:
        if inv.get(p["approval_field"]) != p["approved_value"]:
            return "reject", "No verified approval from debtor"

    if p["reject_duplicates"]:
        if inv.get(p["invoice_id_field"]) in certified_ids:
            return "reject", "Duplicate of already-certified invoice"

    return "certify", "Amount within PO, approved, and not a duplicate"
