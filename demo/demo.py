"""Task 4 — Kapwork x GRAFOMEM demo driver. Four labeled, individually-runnable beats.

  python demo.py --beat 1   Verify & catch fraud (5 certified / 3 rejected)
  python demo.py --beat 2   The certified, signed package (decision_record + receipt)
  python demo.py --beat 3   Tamper-evidence (alter one signed field -> valid:false)
  python demo.py --beat 4   Independent funder verification (fetched key -> valid:true; wrong key -> valid:false)
  python demo.py --beat all

Vocabulary: signed, tamper-evident, independently verifiable, demonstrated-on-sample.
"""
from __future__ import annotations

import argparse
import base64
import json
import os

from common import BASE, client, load_creds
from verify_agent import load_invoices, run_agent, strip_annotations, summary

RULE = "─" * 72


def _clean_invoice(invoice_id: str | None = None) -> dict:
    """Pick a clean (certifiable) invoice from the dataset, annotations stripped.
    Defaults to the Nokia invoice for the narration."""
    invs = load_invoices()
    if invoice_id:
        return next(strip_annotations(i) for i in invs if i["invoice_id"] == invoice_id)
    return next(strip_annotations(i) for i in invs
                if i["invoice_amount"] <= i["po_amount"] and i.get("approval_status") == "approved")


def _one_certified(api_key: str) -> dict:
    """Certify one clean invoice via the SERVER-SIDE rules engine; return {decision_record,
    execution_receipt, …}. The server decides + signs — we just submit the invoice."""
    inv = _clean_invoice("INV-2026-04840")   # Nokia of America, clean
    with client(api_key) as c:
        r = c.post("/v1/governed/verify-batch", json={"invoices": [inv]})
        r.raise_for_status()
        return r.json()["results"][0]


def _verify(receipt: dict, public_key_b64: str | None) -> dict:
    with client() as c:  # no auth — verification is open
        r = c.post("/v1/gcrumbs/verify", json={"receipts": [receipt], "public_key_b64": public_key_b64})
        r.raise_for_status()
        return r.json()


def beat1(api_key: str) -> None:
    print(f"{RULE}\nBEAT 1 — Verify & catch fraud\n{RULE}")
    invoices = load_invoices()
    results = run_agent(api_key, invoices)
    summary(results)


def beat2(api_key: str) -> None:
    print(f"{RULE}\nBEAT 2 — The certified package (this is the Receivables Report entry — and it's signed)\n{RULE}")
    body = _one_certified(api_key)
    dr, rc = body["decision_record"], body["execution_receipt"]
    print(json.dumps(body, indent=2))
    print(f"\n  decision_id      : {dr['decision_id']}")
    print(f"  receipt_id       : {rc['receipt_id']}")
    print(f"  receipt signature: {rc['signature'][:44]}…  (Ed25519)")
    print(f"  signed by key    : {rc['public_key']}  (matches GET /v1/gcrumbs/verify/key)")


def beat3(api_key: str) -> None:
    print(f"{RULE}\nBEAT 3 — Tamper-evidence (any change to a signed field is detected — demonstrated on this sample)\n{RULE}")
    receipt = _one_certified(api_key)["execution_receipt"]
    before = _verify(receipt, receipt["public_key"])
    print(f"  honest receipt              -> valid: {before['valid']}")
    tampered = dict(receipt)
    original = tampered["output_hash"]
    tampered["output_hash"] = ("0" * len(original)) if original[0] != "0" else ("1" * len(original))
    print(f"  altered one signed field (output_hash):\n    {original}\n    -> {tampered['output_hash']}")
    after = _verify(tampered, tampered["public_key"])
    print(f"  tampered receipt            -> valid: {after['valid']}")
    print(f"  reason                      : {after['results'][0]['reason']}")


def beat4(api_key: str) -> None:
    print(f"{RULE}\nBEAT 4 — Independent funder verification (no Kapwork access; wrong key rejected)\n{RULE}")
    receipt = _one_certified(api_key)["execution_receipt"]
    with client() as c:
        key = c.get("/v1/gcrumbs/verify/key").json()
    print(f"  fetched key (GET /v1/gcrumbs/verify/key): {key['public_key_b64']}  [{key['algorithm']}]")
    ok = _verify(receipt, key["public_key_b64"])
    print(f"  verify with the fetched real key -> valid: {ok['valid']}  ({ok['results'][0]['reason']})")
    # A different, unrelated Ed25519 public key must be rejected.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    wrong = base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    bad = _verify(receipt, wrong)
    print(f"  verify with a WRONG key          -> valid: {bad['valid']}  ({bad['results'][0]['reason']})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--beat", default="all", choices=["1", "2", "3", "4", "all"])
    args = ap.parse_args()
    creds = load_creds()
    key = creds["api_key"]
    print(f"BASE = {BASE}  |  tenant {creds['tenant_id']}\n")
    beats = {"1": beat1, "2": beat2, "3": beat3, "4": beat4}
    for b in (["1", "2", "3", "4"] if args.beat == "all" else [args.beat]):
        beats[b](key)
        print()


if __name__ == "__main__":
    main()
