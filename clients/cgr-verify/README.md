# @gns-foundation/cgr-verify

Offline, dependency-light verifier for **GNS Foundation CGR attestations**. Verify a Foundation-signed reputation attestation against a **pinned issuer public key** — **without trusting any grafomem server.** Verification is the consumer's; this library never calls out.

Accepts `cgr.attestation.v1 / v2 / v3` via [`verifyCGRAttestation`](#use), and `cgr.attestation.v4`
(relation edges, traversal, enforcing/non-enforcing modes) via
[`verifyCGRAttestationV4`](#v4--relation-edges-traversal-and-modes). Deps: `@noble/ed25519`,
`@noble/hashes`, `canonicalize`.

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

## v4 — relation edges, traversal, and modes

`cgr.attestation.v4` adds signed **relation edges** (`relates_to`: `continues` / `supersedes` /
`revokes`), a grounding gate, and governance/temporal fields. Verify it with a **separate async entry
point** — `verifyCGRAttestation` and v1–v3 are unchanged.

```js
import { verifyCGRAttestationV4 } from '@gns-foundation/cgr-verify';

// NON-ENFORCING: verify signature, structure, and lineage; honour only edges HANDED to you.
const res = await verifyCGRAttestationV4(subject, ledger, PINNED, {
  mode: 'non-enforcing',
  heldEdges,   // optional: Foundation-signed edge-records you already hold
});
if (!res.valid) throw new Error(`untrusted: ${res.reason}`);
// res.lineage_status — 'complete' | 'truncated_unavailable' | 'truncated_depth' | 'anomaly_cycle'
// res.superseded    — true if a supersedes edge targets the subject (valid, but not current)
```

`ledger` (`{ attestations, delegation_certs }`, both keyed by hash) is the resolution context for the
subject's **own** edges during traversal. Absent targets degrade lineage (they never fail a
`continues`); an incomplete **validity** chain (`supersedes`/`revokes`) fails closed.

### `mode` is explicit and required

There is **no default** — you MUST pass `'enforcing'` or `'non-enforcing'`; a missing or invalid mode
throws `TypeError`. This is deliberate (GNS decision 0006, "enforce-or-label"): a verifier that
silently defaulted could **claim to enforce revocation while enforcing nothing**. Choosing the mode is
the caller's decision, not the library's.

- **non-enforcing** — verifies the subject and honours revocation/supersession **only** via edges you
  already hold (`heldEdges`). It does not go looking.
- **enforcing** — additionally *seeks* revocation/supersession edges targeting the subject and **fails
  closed** if it cannot determine status (a revoked subject must never read as valid). Requires `seek`.

### `seek` — the reverse-index query (you implement it)

Enforcing mode calls an injected `seek` **you** provide:

```ts
seek: (subjectFingerprintHex: string) => Promise<AttestationEdgeRecord[]>
```

Given the subject's BLAKE2b-256 fingerprint (`attestationFingerprint(subject)`), return the
Foundation-signed edge-records (`revokes`/`supersedes` attestations) whose `relates_to` targets it.
This is a **query against your own store/index** — implementing it is the consumer's job; the library
ships none. If `seek` throws, the verifier returns `valid: false` with "revocation status
undeterminable" (fails closed — a DB hiccup must not silently downgrade enforcement).

```js
const res = await verifyCGRAttestationV4(subject, ledger, PINNED, {
  mode: 'enforcing',
  seek: async (fp) => myStore.edgeRecordsTargeting(fp),   // YOUR reverse-index query
});
```

### Caveat: enforcing mode needs a reverse index nobody has yet

Enforcing mode presumes a **queryable reverse index** (`target_fingerprint → edge-records`). As of GNS
decision **0007**, *no* consumer has one — not the reference consumers, not the issuer-side read
surface (whose store can't index encrypted attestation metadata). Until such an index exists,
**enforcing mode is not usefully implementable against a real store, and most consumers should use
non-enforcing mode.** That is honest about what you can do today: verify signatures, structure,
lineage, and *held* edges now; querying for revocation at read time waits on the index.

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
