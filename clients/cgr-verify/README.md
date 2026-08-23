# @gns-foundation/cgr-verify

Offline, dependency-light verifier for **GNS Foundation CGR attestations**. Verify a Foundation-signed reputation attestation against a **pinned issuer public key** — **without trusting any grafomem server.** Verification is the consumer's; this library never calls out.

Accepts `cgr.attestation.v1 / v2 / v3`. Deps: `@noble/ed25519`, `canonicalize`.

## Install

```bash
npm install @gns-foundation/cgr-verify
```

## Use

```js
import { verifyCGRAttestation } from '@gns-foundation/cgr-verify';

// PIN the Foundation issuer key out-of-band (config/env) — do NOT fetch it at runtime.
const PINNED = process.env.CGR_FOUNDATION_PUBKEY; // e.g. e7805ce0…2893ee

const res = await verifyCGRAttestation(attestation, PINNED, {
  expectedKey: agentSubjectKey,   // optional identity binding
  // maxAgeMs: 30 * 86400_000,    // optional freshness gate
});

if (!res.valid) throw new Error(`untrusted: ${res.reason}`);
// score is only present on success, read from the now-verified body:
console.log(res.score, res.evidenceMass, res.nResolved,          // pooled — backs the score
            res.requestedDomain, res.domainNResolved,             // domain match
            res.scoringScope,                                     // "pooled" — NOT a per-domain score
            res.lastResolvedAt);                                  // freshness (signed)
```

**`scoring_scope: "pooled"`** is the honest-scope marker: `score`/`n_resolved` pool all of the subject's judgment evidence into one dimension. `requested_domain` + `domain_n_resolved` describe which capability domain was matched and how much evidence backs *that match* — they do **not** make the score per-domain (per-domain scoring is a later phase). Both scope fields are inside the signed body, so a stripped/edited envelope cannot fake a domain-specific claim.

## The verify recipe (language-agnostic — reimplement in any language)

The attestation is a flat JSON object. To verify without this library:

1. **Signed body** = the attestation minus the two envelope keys `signature` and `evidence_ref`.
2. **Canonicalize** the signed body with **RFC 8785 (JCS)** → UTF-8 bytes. (JS `canonicalize`, Python `rfc8785` — byte-identical; the committed golden fixtures lock this cross-language.)
3. **Ed25519-verify** `signature` (hex) over those **raw canonical bytes** — **NO SHA-512 prehash** — under the **pinned** Foundation public key (hex).
4. Reject unless, additionally: `schema ∈ {cgr.attestation.v1,v2,v3}`, `issuer == "gns-foundation"`, `issuer_key_id == <pinned key>`, and `subject_key != issuer_key_id` (neutrality). If binding an identity, require `subject_key == <expected>`.

Two footguns the recipe pins: (a) **exclude exactly** `signature` + `evidence_ref` before canonicalizing; (b) Ed25519 over the **raw** canonical bytes (not a hash of them).

Golden fixtures for cross-language parity: [`fixtures/`](fixtures/) (`cgr_attestation_v2/v3_jcs.golden.json`) carry the exact canonical bytes + a signature under a known test key.

## Test

```bash
npm test
```
Verifies the golden fixtures (incl. JS↔Python JCS parity), every-field tamper detection, wrong-key rejection, v2 backward-compat, and identity binding.

## Guarantees / non-goals

- **You** pin the key and **you** verify — the Foundation signs only; grafomem serves but is never trusted.
- Continuity (key rotation) is verified separately from the rotation proofs (`/v1/cgr/rotations`); this library verifies the base attestation signature + binding.
- License: MIT.
