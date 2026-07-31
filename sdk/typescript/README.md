# grafomem-cloud (TypeScript / JavaScript)

Official TS/JS client for GRAFOMEM Cloud — governed decisions, signed execution
receipts, and independent (funder-side) verification. Uses the platform `fetch`
(Node 18+, Deno, browsers).

```bash
cd sdk/typescript && npm install && npm run build
```

```ts
import { GrafomemClient } from "grafomem-cloud";

const BASE = "https://grafomem-staging-staging.up.railway.app";

// 1. Onboard a tenant. Or: new GrafomemClient(BASE, "gfm_…")
const { client, info } = await GrafomemClient.signup(BASE, { name: "Acme", email: "ops@acme.io", password: "…" });

// 2. Ingest invoices — verification runs SERVER-SIDE, every result is signed.
const out = await client.verifyBatch([
  { invoice_id: "INV-1", po_amount: 142000, invoice_amount: 142000, approval_status: "approved" },
  { invoice_id: "INV-2", po_amount: 95000,  invoice_amount: 128400, approval_status: "approved" },
]);
console.log(out.summary);                    // { total: 2, certified: 1, rejected: 1 }
const receipt = out.results[0].execution_receipt;

// 3. A funder verifies a receipt independently — no api key.
const anon = new GrafomemClient(BASE);
const { public_key_b64 } = await anon.publicKey();
console.log((await anon.verify([receipt], public_key_b64)).valid);   // true
```

### Bring your own field names (no data transform)

Pass a `policy` (2nd arg) that names **your** invoice fields — rules,
de-duplication, and the result echo all follow it:

```ts
const out = await client.verifyBatch(myInvoices, {
  invoice_amount_field: "invoiceAmount",   // → compared to the PO amount
  po_amount_field:      "poAmount",
  approval_field:       "approvalState",
  approved_value:       "APPROVED",         // matched exactly
  invoice_id_field:     "invoiceNumber",    // de-duplication + echoed as invoice_id
  vendor_field:         "vendorName",
  debtor_field:         "debtorName",
});
```

Amount fields must be numeric (parse currency strings first).

**Methods:** `signup` · `verifyBatch` · `governedDecision` · `publicKey` · `verify` · `readyz`.
Non-2xx responses throw `GrafomemError(status, body)`. This is the same primitive a
browser/funder integration uses to verify a certification client-side.
