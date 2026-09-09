# Changelog

All notable changes to `@gns-foundation/cgr-verify`.

## 0.5.0 — 2026-09-09

Implements decision **0011** in `verifyCosign` — issuer key pinning (§8.2a) and uniform
unresolvable across operators. **Minor bump:** `verifyCosign` gains a **required** 4th
argument (the trusted issuer set); a call that omits it now fails closed (`no trusted issuer
set`) rather than trusting the record's self-declared issuer. Attestation v1–v4 unchanged.

### Changed
- `verifyCosign(record, registry, ledger, trustedIssuers)` — new **required** `trustedIssuers`
  set (array or `Set` of issuer key ids; `ed25519:<hex>` or bare hex). **No default, no
  trust-everything path:** absent or empty ⇒ reject. New ordered step **2a** (after profile
  resolution, before the system-signature check) rejects `issuer_untrusted` when the record's
  `system_metadata.issuer_key_id` is not in the trusted set — self-declaration alone is
  insufficient (decision 0011 gap 1).
- §5.1 `in` operator with a **non-array** `value` now resolves **UNDETERMINED** → reject
  `predicate_unresolved` (previously read as predicate-false — a fail-open; decision 0011
  gap 2, corpus vector `U4`). Brings the JS verifier into line with the Python one.
- `bin/verify-cosign.mjs` threads `trusted_issuers` from the stdin payload.

Passes the full 28-vector `conformance/cgr-cosign-v1/` corpus (incl. `I1`/`I2`/`U4`/`R5`/`R6`),
0 verdict disagreements with the Python reference verifier.

### Also in this release (publish prep — `verifyCosign` reaches consumers for the first time)
- **`index.d.ts` now types `verifyCosign`** and its `CosignRegistry` / `CosignProfile` /
  `CosignPredicate` / `CosignLedger` / `TrustedIssuers` / `VerifyCosignResult` types. The export
  existed in `index.js` since 0.4.0 but was untyped (`.d.ts` drift).
- **README** documents co-signed records, the required trusted-issuer set, the **registry contract
  and a minimal working example** (there is no published registry yet — §11 Q3), and the honest
  limits (key-not-person / gap 3a; the strippability register; assurance tier **surfaced, not gated**).
- **`test/smoke.cosign.test.mjs`** — a shipped, fixture-free smoke test (mints from seeds): valid
  record, stripped-approver-sig → nesting fail, the required-trusted-set contract, and issuer pinning.

> **Note — first published release with `cosign`.** 0.4.0 was prepared but **never published to npm**
> (latest published is 0.3.0). So for a consumer upgrading from 0.3.0, `verifyCosign` is **entirely
> new** and its trusted-issuer argument is **required from the outset** — there is no prior 3-argument
> call to migrate. The attestation API (`verifyCGRAttestation`, `verifyCGRAttestationV4`) is unchanged.

## 0.4.0 — 2026-09-08 (unpublished)

Adds a `cgr.cosign.v1` verifier (`verifyCosign`, `src/cosign.js`) — the two-party
co-signature envelope (`docs/cgr/cgr-cosign-v1-spec.md`, amended by decision 0010:
predicate-unresolved → reject). **Minor bump:** additive (a new export + a new CLI
`bin/verify-cosign.mjs`); attestation v1–v4 verification is unchanged.

### Added
- `verifyCosign(record, registry, ledger)` — §8 ordered checks, failing closed: schema,
  profile resolution (unknown/mode-mismatch), content integrity (BLAKE2b-256 over JCS),
  predicate required-ness (unresolved → reject, per 0010), approver signature (over
  `DOMAIN_TAG ‖ JCS(assertion)`, independently verifiable), system signature (over the body
  **including** `approver_signature` — catches stripping), nonce replay, act/draft
  consistency, free-mode reference well-formedness (§8.9 degrade). Surfaces
  `approver_id`/`approver_act`/`decision_date`/`assurance_tier` and **does not gate** on the tier.
- `bin/verify-cosign.mjs` — conformance bridge driven by `conformance/cgr-cosign-v1/`.
- Passes all 23 corpus vectors (`CGR_COSIGN_VERIFIER=… pytest tests/test_cosign_conformance.py`).
  Implementing it surfaced a corpus correction (N1/N2 now isolate the §8.6 nesting check).

## 0.3.0 — 2026-09-02

Adds `evidence_tier` on the `continues` edge (spec §1.1). **Minor bump, not patch**: the field is
additive *to the wire* (a deployed v4 verifier carries it as an unread signed field, so published
`0.2.0` was undisturbed), but this verifier gains **new rejection behaviour** and a new surfaced
result field — a behaviour change for consumers who upgrade.

### Added
- `verifyCGRAttestationV4` **surfaces** `evidence_tier` in its result — the authority tier the
  Foundation attests substantiated a `continues` edge (a recorded *claim*, not proof; **non-gating**).
  New `EvidenceTier` type; `evidence_tier?` on `VerifyResultV4`.

### Changed — verification behaviour (this is why it's a minor, not a patch)
- A `continues` edge now **MUST** carry a valid `evidence_tier` from the closed vocabulary
  `custody_record` / `issuer_records` / `operator_verification`. The verifier now **rejects**: a
  `continues` missing it; any edge with an out-of-vocabulary value; a `supersedes`/`revokes` carrying
  it. An attestation that verified under `0.2.0` but breaks these rules now returns `valid: false`.
  (No such attestations exist yet — v4 issuance has not started — but the contract is stricter.)
- The verifier does **NOT** gate on *which* tier; sufficiency is the relying party's call.

### Note
- Published `0.2.0` does not have this. Consumers who need `evidence_tier` (its surfacing or its
  enforcement) must upgrade to `0.3.0`.

## 0.2.0 — 2026-09-01

Adds `cgr.attestation.v4` verification. **Purely additive**: 0.1.0 shipped v1/v2/v3 only, so v4 is
new to consumers and no existing API changed. Minor bump.

### Added
- **`verifyCGRAttestationV4(subject, ledger, pinnedIssuer, opts)`** — verify a `cgr.attestation.v4`
  attestation offline. Covers relation-edge (`relates_to`) validation, `continues`/validity
  traversal (Lineage-Degrades / Validity-Fails-Closed), the `lineage_status` signal
  (`complete` / `truncated_unavailable` / `truncated_depth` / `anomaly_cycle`), the grounding gate,
  held + sought edge honouring, and `superseded` (distinct from `valid: false`).
- **Enforcing and non-enforcing modes.** `opts.mode` is **explicit and REQUIRED** — there is no
  default (a missing/invalid mode throws `TypeError`), per GNS decision 0006 ("enforce-or-label"): a
  verifier that silently defaulted could claim to enforce revocation while enforcing nothing.
- **Injected `seek`** (required iff `mode === 'enforcing'`) — the consumer's reverse-index query for
  edge-records targeting the subject. If `seek` throws, verification fails closed with
  "revocation status undeterminable". Enforcing without `seek` throws `TypeError`.
- Exports: `attestationFingerprint`, `V4_SCHEMA`, `GROUNDING_DIMENSIONS`. Types: `EnforcementMode`,
  `V4Ledger`, `VerifyV4Options`, `VerifyResultV4`, `LineageStatus`.

### Unchanged
- v1/v2/v3 verification (`verifyCGRAttestation`) — identical signature and behaviour; existing
  consumers are unaffected.

### Note — enforcing mode is not usefully implementable yet
Enforcing mode presumes a **queryable reverse index** (`target_fingerprint → edge-records`). As of
GNS decision **0007**, no consumer has one — not the reference consumers, not the issuer-side read
surface. Until such an index exists, most consumers should use **non-enforcing** mode. See the README.

## 0.1.0

- Initial release: offline verification of `cgr.attestation.v1/v2/v3` against a pinned Foundation
  issuer key (RFC 8785 / JCS + Ed25519 over raw canonical bytes), identity binding, and freshness.
