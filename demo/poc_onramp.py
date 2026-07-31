#!/usr/bin/env python3
"""POC on-ramp — 'bring your batch, watch it run'.

Takes a JSON file of KAPWORK-SHAPED invoices, maps them to the GRAFOMEM
verify-batch schema, runs the batch on a FRESH staging tenant, prints
certified/rejected with reasons, confirms one receipt verifies independently,
and reports timing + how to see it in the /portal Audit Console.

    pip install ./sdk/python
    export GRAFOMEM_BASE=https://grafomem-staging-staging.up.railway.app
    python demo/poc_onramp.py demo/kapwork_sample.json
"""
import json, os, sys, time
from grafomem_cloud import GrafomemClient

# Kapwork field  ->  GRAFOMEM canonical field. Edit to match their export.
FIELD_MAP = {
    "invoiceNumber": "invoice_id", "vendorName": "vendor", "debtorName": "debtor",
    "poAmount": "po_amount", "invoiceAmount": "invoice_amount", "approvalState": "approval_status",
}
DEMO_PW = "Poc-2026!"   # synthetic throwaway tenant password

def to_canonical(k: dict) -> dict:
    out = {FIELD_MAP[s]: k[s] for s in FIELD_MAP if s in k}
    if "approval_status" in out:                      # normalize to the value the rules expect
        out["approval_status"] = str(out["approval_status"]).strip().lower()
    return out

def main():
    base = os.environ.get("GRAFOMEM_BASE") or sys.exit("set GRAFOMEM_BASE (staging url)")
    path = sys.argv[1] if len(sys.argv) > 1 else "demo/kapwork_sample.json"
    invoices = [to_canonical(k) for k in json.load(open(path))]

    email = f"kapwork-poc+{int(time.time())}@kapwork.example"
    client, info = GrafomemClient.signup(base, name="Kapwork POC", email=email, password=DEMO_PW)
    print(f"fresh tenant {info['tenant_id']}   portal login: {email} / {DEMO_PW}")

    t0 = time.time(); out = client.verify_batch(invoices); dt = time.time() - t0
    s = out["summary"]
    print(f"\nBatch: {s['total']} invoices  ->  {s['certified']} certified, {s['rejected']} rejected   ({dt:.2f}s, {dt/max(s['total'],1)*1000:.0f} ms/invoice)")
    for r in out["results"]:
        print(f"  [{r['decision'].upper():7}] {str(r['invoice_id']):<10} {str(r['vendor'] or ''):<12} -> {r['reason']}")

    print(f"\nRecorded in Decision Trail: {client.list_decisions(limit=200).get('count')} decisions "
          f"— log into /portal as the tenant above to see them live.")
    rc = out["results"][0]["execution_receipt"]
    anon = GrafomemClient(base); key = anon.public_key()["public_key_b64"]
    print("Independent receipt verify -> valid:", anon.verify([rc], public_key_b64=key)["valid"])

if __name__ == "__main__":
    main()
