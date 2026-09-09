# @gns-foundation/cgr-verify

Offline, dependency-light verifier for **GNS Foundation CGR attestations**. Verify a Foundation-signed reputation attestation against a **pinned issuer public key** — **without trusting any grafomem server.** Verification is the consumer's; this library never calls out.

Accepts `cgr.attestation.v1 / v2 / v3` via [`verifyCGRAttestation`](#use), and `cgr.attestation.v4`
(relation edges, traversal, enforcing/non-enforcing modes) via
[`verifyCGRAttestationV4`](#v4--relation-edges-traversal-and-modes). It also verifies the
**`cgr.cosign.v1` two-party co-signature envelope** (a human approval bound to an agent decision)
via [`verifyCosign`](#cosign--two-party-co-signature-envelope-verifycosign). Deps: `@noble/ed25519`,
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
// res.evidence_tier — on a continues edge: the authority tier the Foundation attests
//                     ('custody_record' | 'issuer_records' | 'operator_verification'). SURFACED, not gated.
```

`ledger` (`{ attestations, delegation_certs }`, both keyed by hash) is the resolution context for the
subject's **own** edges during traversal. Absent targets degrade lineage (they never fail a
`continues`); an incomplete **validity** chain (`supersedes`/`revokes`) fails closed.

### `evidence_tier` — the authority behind a `continues` (v0.3+)

A `continues` edge **must** carry `evidence_tier` — which authority substantiated the rotation claim,
from a closed vocabulary in descending strength: `custody_record`, `issuer_records`,
`operator_verification`. The verifier **rejects** a `continues` missing it, an out-of-vocabulary
value, or a `supersedes`/`revokes` that carries it; and it **surfaces** the value (`res.evidence_tier`)
but **does not gate on which tier** — whether `operator_verification` is sufficient is *your* call.
It is the Foundation's attested **claim** about its own evidence, **not proof**: `custody_record` means
"the Foundation attests it relied on a custody record," not that custody was cryptographically verified.

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

## cosign — two-party co-signature envelope (`verifyCosign`)

`cgr.cosign.v1` is a **two-party co-signature envelope**: an agent produces a decision, a **human
approver** signs an assertion binding their approval to *that* decision's content, and the issuing
system counter-signs a record that **includes** the approver's signature. It exists because a defensible
approval needs two parties — the decider and the recorder — where the base attestation has one. Verify
one offline:

```js
import { verifyCosign } from '@gns-foundation/cgr-verify';

// The trusted issuer set is REQUIRED — you supply it, out-of-band, like the pinned attestation key.
const TRUSTED_ISSUERS = [process.env.CGR_COSIGN_ISSUER_KEY]; // ['ed25519:<hex>'] or ['<hex>']

const res = await verifyCosign(record, registry, ledger, TRUSTED_ISSUERS);
if (!res.valid) throw new Error(`rejected: ${res.reason}`);   // e.g. 'issuer_untrusted', 'predicate_unresolved'
// res.surfaced — { approver_id, approver_act, decision_date, assurance_tier } — SURFACED, NOT gated
```

### The trusted issuer set is REQUIRED — no default, no fallback

`trustedIssuers` (4th arg) is the set of issuer keys **you** trust to have issued a co-signed record.
It is **required, with no default and no trust-everything path**: an absent or empty set makes the
verifier **reject**, never fall back to the issuer the record declares about itself. This is deliberate
(decision [0011](https://github.com/GNS-Foundation/grafomem/blob/main/docs/decisions/0011-cosign-issuer-trust-and-uniform-unresolved.md),
§8.2a): the system signature is verified against `system_metadata.issuer_key_id` — **the key the record
names itself** — which proves only internal consistency, not that a system *you* trust issued it. Without
pinning, a record verifies as "valid" under **any** issuer key that signs it consistently: the approver
binding would hold while the issuer binding did not. `verifyCosign` checks the issuer against your set
**before** it spends a signature verification, and rejects `issuer_untrusted` if it is not in it.

### The registry — profile resolution (you must supply it)

`verifyCosign` resolves `record.profile` in a **registry** you pass. A profile fixes the record's
`approval_mode` and **when the approver signature is required**. Its shape:

```jsonc
{
  "profiles": {
    "your.profile.name": {
      "approval_mode": "bound",           // "bound" (approval binds this exact content) | "free"
      "approver_signature": "REQUIRED",   // unconditional; OR a predicate (below)
      "referenced_records": "none"        // "none" | "required" (free mode references other records)
    },
    "your.risk.gated": {
      "approval_mode": "bound",
      // required ONLY when a (field, op, literal) predicate over content_body holds (§5.1):
      "approver_signature": { "required_when": { "field": "risk_class", "op": "in", "value": ["high", "critical"] } },
      "referenced_records": "none"
    }
  }
}
```

`op` is one of `eq`, `ne`, `in`, `lt`, `lte`, `gt`, `gte` (`in` takes an **array** `value`). An
**unresolvable** predicate — a missing/null/non-scalar field, a non-numeric ordering operand, or a
**non-array `in` value** — is UNDETERMINED and **rejects** (`predicate_unresolved`); it never silently
degrades to "not required" (that was a fail-open; decisions 0010/0011).

> **⚠️ There is no published profile registry yet.** The registry *document* is an open spec question
> (§11 Q3) — the profiles above are the **shape and a minimal working example**, not a standard set of
> profile names. The registry fixture under
> [`conformance/cgr-cosign-v1/`](https://github.com/GNS-Foundation/grafomem/tree/main/conformance/cgr-cosign-v1)
> is a **test fixture** (it carries a loud in-file `_WARNING` saying exactly that) — do not import it as
> the registry. Until the registry is standardised, **you author your own profiles** for the records you
> issue and verify; keep the names and predicates stable, because they are part of what the signatures
> are checked against.

### `ledger` — replay and free-mode resolution

`ledger` (`{ seen?, resolvable? }`) is optional. `seen` is the set of `[approver_key_id, record_nonce]`
pairs already observed — a duplicate is rejected as a replay (§4). `resolvable` lists reference hashes a
**free**-mode record may point at; a well-formed but unresolvable reference **degrades** (surfaces
`references_unresolved`, still valid), it does not reject.

### Two placements

- **In your service (library import).** `import { verifyCosign } from '@gns-foundation/cgr-verify'` and
  call it at the point a co-signed decision is relied upon — supplying your trusted issuer set and your
  registry. This is the normal path.
- **As a language-neutral contract.** The envelope's rules are pinned by the **28-vector conformance
  corpus** at [`conformance/cgr-cosign-v1/`](https://github.com/GNS-Foundation/grafomem/tree/main/conformance/cgr-cosign-v1)
  (decisions 0010 + 0011) and the [spec](https://github.com/GNS-Foundation/grafomem/blob/main/docs/cgr/cgr-cosign-v1-spec.md)
  §8. A second, independent Python verifier (`grafomem-cgr[verify]`) meets the same corpus with zero
  verdict disagreements. Re-implement in any language against the corpus, not against prose.

### Honest limits — read these before you rely on a `valid`

- **The envelope attributes a KEY, not a verified person.** A `valid` result proves an `approver_key_id`
  signed the approval — **not** that the key belongs to a named, accountable, identity-proofed human.
  Binding a key to a real person (KYC/enrolment, or a QES co-signature) is a **separate assurance layer**
  the envelope does not provide (spec gap 3a, §9/§10).
- **Tamper-evident, in a specific register — not tamper-proof.** The system signature covers a body that
  *includes* the approver signature, so **stripping or altering it makes the record invalid** (the
  nesting property); a **required** approver signature that is simply **absent** makes the record
  **non-conformant** (rejected). What the envelope does **not** prevent is a compromised issuer
  **re-minting** a fresh record — that is **detectable** (via the external audit chain), not preventable
  by verifying a single record.
- **The assurance tier is SURFACED, never GATED.** `res.surfaced.assurance_tier` reports how strongly the
  approver key is bound to a person — and the verifier **does not gate on it**. **A `valid` verdict does
  not mean "high assurance."** Whether a given tier is *sufficient* for your decision is **your**
  enforcement policy, not the library's (§8). Consumers routinely assume "valid" implies "sufficiently
  identified" — it does not; you must check the tier yourself if your use requires one.

## The verify recipe (v1/v2/v3) — language-agnostic (reimplement in any language)

The attestation is a flat JSON object. Steps 1–3 (the signature over the JCS-canonical body) are the
**shared base for every version, `v4` included**; step 4's acceptance set is `v1/v2/v3` (`v4`
re-implementers, see [Re-implementing v4](#re-implementing-v4) below). To verify a `v1/v2/v3`
attestation without this library:

1. **Signed body** = the attestation minus the two envelope keys `signature` and `evidence_ref`.
2. **Canonicalize** the signed body with **RFC 8785 (JCS)** → UTF-8 bytes. (JS `canonicalize`, Python `rfc8785` — byte-identical; the committed golden fixtures lock this cross-language.)
3. **Ed25519-verify** `signature` (hex) over those **raw canonical bytes** — **NO SHA-512 prehash** — under the **pinned** Foundation public key (hex).
4. Reject unless, additionally: `schema ∈ {cgr.attestation.v1,v2,v3}`, `issuer == "gns-foundation"`, `issuer_key_id == <pinned key>`, and `subject_key != issuer_key_id` (neutrality). If binding an identity, require `subject_key == <expected>`.

Two footguns the recipe pins: (a) **exclude exactly** `signature` + `evidence_ref` before canonicalizing; (b) Ed25519 over the **raw** canonical bytes (not a hash of them).

### Re-implementing v4

`v4` shares steps 1–3 **unchanged** (same signed body, same JCS + raw-bytes Ed25519), then **replaces
step 4** with the relation-edge machinery in [§1 of the spec](https://github.com/GNS-Foundation/grafomem/blob/main/docs/cgr/cgr-attestation-v4-spec.md):
schema string `cgr.attestation.v4`; `relates_to` per-edge validation (per-kind `hash_alg`,
multiplicity), `continues`/validity **traversal** (Lineage-Degrades / Validity-Fails-Closed), the
`lineage_status` signal, the grounding gate, held/sought edges, and the `mode` contract (see
[v4](#v4--relation-edges-traversal-and-modes) above). Those rules are extensive and normative, so this
README does **not** restate them — the authoritative, **testable** definition is the spec plus the
language-neutral [conformance corpus](https://github.com/GNS-Foundation/grafomem/tree/main/conformance/cgr-attestation-v4)
(38 vectors, both modes). Verify a re-implementation against the corpus, not against prose.

Golden fixtures for cross-language parity: [`fixtures/`](https://github.com/GNS-Foundation/grafomem/tree/main/clients/cgr-verify/fixtures) (`cgr_attestation_v2/v3_jcs.golden.json`) carry the exact canonical bytes + a signature under a known test key.

## Test

```bash
npm test
```
Runs three suites:
- **v1/v2/v3** (`test/verify.test.mjs`) — the golden fixtures (incl. JS↔Python JCS parity), every-field tamper detection, wrong-key rejection, v2 backward-compat, and identity binding.
- **v4 smoke** (`test/smoke.v4.test.mjs`) — a self-contained, fixture-free check that the shipped v4 code runs: both modes, a held `revokes` (fail), a `continues` `lineage_status`, and `seek` throwing → fail closed.
- **cosign smoke** (`test/smoke.cosign.test.mjs`) — fixture-free: mints co-signed records from known seeds and checks a valid two-signature record, a stripped approver signature (→ system signature fails, the nesting property), the **required-trusted-set contract** (omitted/empty → reject — the fail-open that must never regress), and **issuer pinning** (a record signed correctly by an untrusted key → `issuer_untrusted`, while the same record under a trusted key verifies).

The smoke tests are **smoke tests, not the contract** — they prove the shipped code runs, they are not exhaustive. The authoritative definitions are the conformance corpora: v4 ([38 vectors](https://github.com/GNS-Foundation/grafomem/tree/main/conformance/cgr-attestation-v4)) and cosign ([28 vectors](https://github.com/GNS-Foundation/grafomem/tree/main/conformance/cgr-cosign-v1)); if a smoke test and its corpus ever disagree, the corpus wins.

## Guarantees / non-goals

- **You** pin the key and **you** verify — the Foundation signs only; grafomem serves but is never trusted.
- Continuity (key rotation) is verified separately from the rotation proofs (`/v1/cgr/rotations`); this library verifies the base attestation signature + binding.
- License: MIT.
