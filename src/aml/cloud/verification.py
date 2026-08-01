"""Server-side invoice-verification rules engine.

Deterministic checks applied to each submitted invoice. The policy is
configurable per request, so a customer expresses their own field names,
thresholds, and which checks are active without a code change — the decision it
returns is then recorded as a signed, tamper-evident governed decision.

evaluate_invoice() returns (decision, reason_code, reason_text) where decision is
"certify" or "reject" and reason_code is a stable code from REASON_CODES (so the
CGR substrate can group calls without NLP). Duplicate detection is caller-scoped:
pass the set of invoice_ids already certified in this batch.
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
    # Echoed on each result (not evaluated) — override to a customer's field names.
    "vendor_field": "vendor",
    "debtor_field": "debtor",
}


def resolve_policy(policy: dict | None) -> dict[str, Any]:
    return {**DEFAULT_POLICY, **(policy or {})}


# Stable, NLP-free grouping keys for the CGR substrate. All `rule`/verifiable.
# (A future `risk_judgment` code will come from a judgment agent — not this layer.)
REASON_CODES = ("amount_exceeds_po", "amount_or_po_missing",
                "no_debtor_approval", "duplicate", "clean")


def evaluate_invoice(inv: dict, policy: dict | None, certified_ids: set) -> tuple[str, str, str]:
    """Apply the (resolved) policy to one invoice.

    Returns (decision, reason_code, reason_text). ``reason_code`` is a stable
    code from REASON_CODES so CGR can group calls without NLP; ``reason_text``
    is the human-readable explanation.
    """
    p = resolve_policy(policy)
    # Ignore any annotation/helper keys (e.g. '_fraud') — decide only from real fields.
    inv = {k: v for k, v in inv.items() if not str(k).startswith("_")}

    if p["require_amount_within_po"]:
        try:
            inv_amt = float(inv.get(p["invoice_amount_field"]))
            po_amt = float(inv.get(p["po_amount_field"]))
        except (TypeError, ValueError):
            return "reject", "amount_or_po_missing", "Invoice or PO amount is missing or non-numeric"
        if inv_amt > po_amt:
            return "reject", "amount_exceeds_po", "Invoice amount exceeds authorized PO"

    if p["require_approval"]:
        if inv.get(p["approval_field"]) != p["approved_value"]:
            return "reject", "no_debtor_approval", "No verified approval from debtor"

    if p["reject_duplicates"]:
        if inv.get(p["invoice_id_field"]) in certified_ids:
            return "reject", "duplicate", "Duplicate of already-certified invoice"

    return "certify", "clean", "Amount within PO, approved, and not a duplicate"
