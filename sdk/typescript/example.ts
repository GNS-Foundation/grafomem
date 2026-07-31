// Minimal end-to-end example for the GRAFOMEM Cloud TS client (Node 18+).
//   cd sdk/typescript && npm install && npm run build && node dist-example.js
// or with ts-node:  node --loader ts-node/esm example.ts
import { GrafomemClient } from "./src/index.js";

const BASE = process.env.GRAFOMEM_BASE ?? "https://grafomem-staging-staging.up.railway.app";

const invoices = [
  { invoice_id: "INV-1", vendor: "Northline", debtor: "Verizon", po_amount: 142000, invoice_amount: 142000, approval_status: "approved" },
  { invoice_id: "INV-2", vendor: "Granite Peak", debtor: "Charter", po_amount: 95000, invoice_amount: 128400, approval_status: "approved" }, // over PO
];

const { client, info } = await GrafomemClient.signup(BASE, {
  name: "Acme Financing", email: `ops+${Math.random().toString(16).slice(2, 8)}@acme.example`, password: "Example-2026!",
});
console.log("tenant:", info.tenant_id);

const out = await client.verifyBatch(invoices);
console.log("summary:", out.summary);
const receipt = out.results[0].execution_receipt;

const anon = new GrafomemClient(BASE);
const { public_key_b64 } = await anon.publicKey();
console.log("verify (real key):", (await anon.verify([receipt], public_key_b64)).valid);

const bad = { ...receipt, output_hash: "0".repeat(receipt.output_hash.length) };
console.log("verify (tampered):", (await anon.verify([bad], public_key_b64)).valid);
