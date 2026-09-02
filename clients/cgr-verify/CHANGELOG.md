# Changelog

All notable changes to `@gns-foundation/cgr-verify`.

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
