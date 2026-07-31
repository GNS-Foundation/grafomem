/**
 * GRAFOMEM Cloud — official TypeScript/JavaScript client.
 *
 * Thin, typed wrapper over the GRAFOMEM Cloud REST API for the governed-decision
 * + signed-receipt + independent-verification flow. Uses the platform `fetch`
 * (Node 18+, Deno, browsers).
 *
 *   import { GrafomemClient } from "grafomem-cloud";
 *   const { client } = await GrafomemClient.signup(BASE, { name, email, password });
 *   const out = await client.verifyBatch(invoices);           // server-side verify + sign
 *   const { public_key_b64 } = await new GrafomemClient(BASE).publicKey();
 *   const v = await new GrafomemClient(BASE).verify([receipt], public_key_b64); // funder check
 */

export interface Invoice { invoice_id?: string; [k: string]: unknown; }
export interface Receipt { receipt_id: string; signature: string | null; [k: string]: unknown; }
export interface VerifyResult { valid: boolean; count: number; results: Array<{ receipt_id?: string; valid: boolean; reason: string }>; }
export interface BatchResult { summary: { total: number; certified: number; rejected: number }; policy: Record<string, unknown>; results: any[]; }

export class GrafomemError extends Error {
  constructor(public status: number, public body: unknown) {
    super(`GRAFOMEM API error ${status}: ${JSON.stringify(body)}`);
  }
}

export class GrafomemClient {
  readonly baseUrl: string;
  private apiKey?: string;

  constructor(baseUrl: string, apiKey?: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
  }

  private async req<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (this.apiKey) headers["X-API-Key"] = this.apiKey;
    const res = await fetch(this.baseUrl + path, {
      method, headers, body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    const parsed = text ? JSON.parse(text) : null;
    if (!res.ok) throw new GrafomemError(res.status, parsed ?? text);
    return parsed as T;
  }

  /** Create a tenant; returns an authenticated client + tenant info (incl. api_key). */
  static async signup(baseUrl: string, opts: { name: string; email: string; password: string; plan?: string }):
    Promise<{ client: GrafomemClient; info: any }> {
    const base = baseUrl.replace(/\/+$/, "");
    const res = await fetch(base + "/v1/portal/signup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: "starter", ...opts }),
    });
    const info = await res.json();
    if (!res.ok) throw new GrafomemError(res.status, info);
    return { client: new GrafomemClient(base, info.api_key), info };
  }

  /** Ingest invoices; verification runs server-side and each result is signed. */
  verifyBatch(invoices: Invoice[], policy: Record<string, unknown> = {}, modelId = "kapwork-verify-agent-v1") {
    return this.req<BatchResult>("POST", "/v1/governed/verify-batch", { invoices, policy, model_id: modelId });
  }

  /** Record one externally-made decision as a signed decision_record + receipt. */
  governedDecision(decision: string, reason = "", invoiceId?: string, context: Record<string, unknown> = {}, modelId = "kapwork-verify-agent-v1") {
    return this.req<any>("POST", "/v1/governed/decisions", { decision, reason, invoice_id: invoiceId, context, model_id: modelId });
  }

  /** The signer's Ed25519 public key. Public endpoint — no auth. */
  publicKey() {
    return this.req<{ algorithm: string; public_key_hex: string; public_key_b64: string }>("GET", "/v1/gcrumbs/verify/key");
  }

  /** Stateless verification of receipts against a public key. No auth, no DB — the funder's check. */
  verify(receipts: Receipt[], publicKeyB64?: string) {
    return this.req<VerifyResult>("POST", "/v1/gcrumbs/verify", { receipts, public_key_b64: publicKeyB64 ?? null });
  }

  readyz() { return this.req<any>("GET", "/readyz"); }
}
