# grafomem-cloud (Python)

Official Python client for GRAFOMEM Cloud — governed decisions, signed execution
receipts, and independent (funder-side) verification.

```bash
pip install ./sdk/python        # or: pip install grafomem-cloud  (when published)
```

```python
from grafomem_cloud import GrafomemClient

BASE = "https://grafomem-staging-staging.up.railway.app"

# 1. Onboard a tenant (self-serve). Or: GrafomemClient(BASE, api_key="gfm_…")
client, info = GrafomemClient.signup(BASE, name="Acme", email="ops@acme.io", password="…")

# 2. Ingest invoices — verification runs SERVER-SIDE, every result is signed.
out = client.verify_batch([
    {"invoice_id": "INV-1", "po_amount": 142000, "invoice_amount": 142000, "approval_status": "approved"},
    {"invoice_id": "INV-2", "po_amount": 95000,  "invoice_amount": 128400, "approval_status": "approved"},
])
print(out["summary"])                       # {'total': 2, 'certified': 1, 'rejected': 1}
receipt = out["results"][0]["execution_receipt"]

# 3. A funder verifies a receipt independently — no api key, no DB access.
anon = GrafomemClient(BASE)
key = anon.public_key()["public_key_b64"]
print(anon.verify([receipt], public_key_b64=key)["valid"])   # True
```

### Bring your own field names (no data transform)

Pass a `policy` that names **your** invoice fields — the rules, duplicate
detection, and the result echo all follow it, so you don't have to reshape your
data before sending it:

```python
out = client.verify_batch(my_invoices, policy={
    "invoice_amount_field": "invoiceAmount",   # your field → compared to the PO amount
    "po_amount_field":      "poAmount",
    "approval_field":       "approvalState",
    "approved_value":       "APPROVED",         # your "approved" value (any string, matched exactly)
    "invoice_id_field":     "invoiceNumber",    # used for de-duplication + echoed as invoice_id
    "vendor_field":         "vendorName",       # echoed on each result
    "debtor_field":         "debtorName",
    # turn checks off if you don't want them:
    # "require_approval": False, "reject_duplicates": False, "require_amount_within_po": False,
})
```

Notes: amount fields must be **numeric** (parse currency strings like `"$142,000"` first);
`approved_value` is matched exactly (case-sensitive).

**Methods:** `signup` · `verify_batch` · `governed_decision` · `list_decisions` ·
`public_key` · `verify` · `readyz`. Non-2xx responses raise `GrafomemError(status_code, body)`.
Run `python sdk/python/example.py` for a full end-to-end demo.
