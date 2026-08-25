# CGR Attestation Read Surface (consumer guide)

*The neutral surface through which an external consumer reads Capability-Grounded Reputation for an agent: **give me the attestation for (subject, domain); let me verify it without trusting you; tell me score, evidence mass, freshness, issuer.***

**Verify first.** Everything here is designed so you never trust our server: fetch a Foundation-signed attestation, then verify it yourself against a **pinned** issuer key with [`@gns-foundation/cgr-verify`](../../clients/cgr-verify) (or the language-agnostic recipe in that README). The server serves; the Foundation signs; you verify.

## Endpoint

```
GET /v1/cgr/read/attestation?subject=<key|did|handle>&domain=<capability-domain>
```

**Authenticated** (tenant `X-API-Key`, scope `cgr:read`). Subject via `?subject=` (auto-detects a 64-hex key, a `did:key:…`, or a handle) or explicitly via `?key=` / `?did=` / `?handle=`. `domain` is optional.

> **Note:** public, unauthenticated serving of attestations is **gated** on a signed public-safe boundary spec and is **not** open on this endpoint. Today it is authenticated-only.

## Response — the attestation envelope

Honest-scope by construction: a **bare score is unobtainable**. `score` is only ever returned inside an envelope that also carries its evidence and freshness, and the authoritative copies are **signed inside** `attestation`.

```jsonc
{
  "surface_version": "cgr-read/1",
  "result": "attestation",
  "attestation": { /* Foundation-signed cgr.attestation.v3 — verify THIS */ },
  "score": 0.8,                 // convenience echo; authoritative copy is signed in `attestation.cgr_score`
  "evidence_mass": 5.0,         // pooled n = α+β — backs the score
  "n_resolved": 3,              // pooled resolved-outcome count — backs the score
  "scoring_scope": "pooled",    // the score pools ALL judgment evidence into one dimension — NOT per-domain
  "requested_domain": "deploy-verification",
  "domain_n_resolved": 2,       // resolved outcomes IN the requested domain — backs the domain MATCH
  "freshness": { "as_of": "…", "last_resolved_at": "…", "age_ms": 123, "stale": false },
  "issuer": { "issuer": "gns-foundation", "issuer_key_id": "<hex>", "schema": "cgr.attestation.v3" },
  "continuity": { "status": "verified|asserted|unverified", "advisory": true },
  "verify": { "recipe_url": "…/cgr/verify/", "lib": "@gns-foundation/cgr-verify", "issuer_pubkey": "<hex>" }
}
```

**No evidence** — unknown subject, or a domain the subject has no captured evidence in:

```jsonc
{ "surface_version": "cgr-read/1", "result": "no_evidence", "reason": "…", "score": null, "evidence_mass": null }
```

Never a default score, never `0.5`, never an empty attestation.

## Two evidence masses (read both)

- **`n_resolved` / `evidence_mass` (pooled)** — how much resolved evidence backs the **score**. The score is the agent's single-dimension CGR.
- **`domain_n_resolved`** — how much resolved evidence backs the **domain match** (the subject has this much resolved evidence tagged in `requested_domain`).

`scoring_scope: "pooled"` states plainly: the score is not domain-specific. Per-domain scoring is a later phase; a response will never imply a per-domain score exists before it does. All three markers are inside the signed body, so an intermediary cannot rewrite a pooled score into a domain-specific one.

## Verify it yourself

```js
import { verifyCGRAttestation } from '@gns-foundation/cgr-verify';
const res = await verifyCGRAttestation(body.attestation, PINNED_FOUNDATION_KEY,
                                       { expectedKey: body.attestation.subject_key });
if (!res.valid) throw new Error(res.reason);   // do not trust score until this passes
```

`continuity` from the envelope is **advisory** — for full key-rotation continuity, fetch `GET /v1/cgr/rotations?current=<key>` and re-walk the chain yourself. See the verifier README for the offline recipe and cross-language golden fixtures.
