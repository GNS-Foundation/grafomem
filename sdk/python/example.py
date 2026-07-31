"""Minimal end-to-end example for the GRAFOMEM Cloud Python client.

    pip install ./sdk/python
    python sdk/python/example.py
"""
import os

from grafomem_cloud import GrafomemClient

BASE = os.environ.get("GRAFOMEM_BASE", "https://grafomem-staging-staging.up.railway.app")

INVOICES = [
    {"invoice_id": "INV-1", "vendor": "Northline", "debtor": "Verizon",
     "po_amount": 142000, "invoice_amount": 142000, "approval_status": "approved"},
    {"invoice_id": "INV-2", "vendor": "Granite Peak", "debtor": "Charter",
     "po_amount": 95000, "invoice_amount": 128400, "approval_status": "approved"},  # over PO
]

# 1. onboard a tenant (self-serve)
client, info = GrafomemClient.signup(BASE, name="Acme Financing",
                                     email=f"ops+{os.urandom(3).hex()}@acme.example",
                                     password="Example-2026!")
print("tenant:", info["tenant_id"])

# 2. ingest invoices — verification runs server-side, results are signed
out = client.verify_batch(INVOICES)
print("summary:", out["summary"])
receipt = out["results"][0]["execution_receipt"]

# 3. a funder verifies a receipt independently — no api key
pub = GrafomemClient(BASE).public_key()["public_key_b64"]
print("verify (real key):", GrafomemClient(BASE).verify([receipt], public_key_b64=pub)["valid"])

# 4. tamper -> rejected
bad = dict(receipt); bad["output_hash"] = "0" * len(receipt["output_hash"])
print("verify (tampered):", GrafomemClient(BASE).verify([bad], public_key_b64=pub)["valid"])

client.close()
