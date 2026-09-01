# `cgr.attestation.v4` conformance corpus

The executable, language-neutral encoding of the **normative MUST rules** in
[`docs/cgr/cgr-attestation-v4-spec.md`](../../docs/cgr/cgr-attestation-v4-spec.md). A `v4` verifier is
**conformant iff** it returns the `expect` verdict on every vector here. Written under P1.3 —
tests-before-verifier (P1.1 spec → P1.2 behaviour → **P1.3 corpus** → P1.4 verifier passes it).

## Files
- **`vectors.json`** — the corpus (generated). Each vector: `{id, clause, spec_lines, title, subject,
  ledger, pinned_issuer, expect}`.
- **`generate.py`** — deterministic generator (repeating-byte **test** issuer key `0x11` / agent key
  `0x22` — **not real keys**). Reuses the production canonicalization so vector bytes match a real
  verifier's. Regenerate: `python3 conformance/cgr-attestation-v4/generate.py`.
- **`issuer.json`** — the pinned test pubkeys.
- **`verify_bridge.py`** — `CGR_V4_VERIFIER` bridge: drives the reference verifier
  (`clients/cgr-verify`, JS) from the Python runner.
- Runner: [`tests/test_v4_conformance.py`](../../tests/test_v4_conformance.py).

**Placeholder target hashes.** Where a target only needs to be a *distinct pointer* (cycle detection,
depth counting, a deliberately-unresolvable target) rather than a real fingerprint, the generator uses
**legible valid 64-hex** placeholders: `cc0a…`/`cc0b…` (**c**ycle nodes), `de0001…`–`de0065…`
(**de**pth chain index), `dead…` (unreachable — absent from the ledger). These MUST be valid lowercase
hex: §1.1's malformed-hash rule rejects non-hex targets, so a non-hex placeholder would be rejected
before the traversal logic it exists to test. *(This was caught by implementing the verifier against
the corpus — see the P1.4 PR.)*

## Resolution model
A verifier is given four inputs:
- **`subject`** — the attestation under evaluation.
- **`ledger`** — `{attestations, delegation_certs}`, a map from `target.hash` to the entry it resolves.
  Two roles: the **resolution context** for traversing the subject's *own* edges, and the **queryable
  index** a liveness "seek" would consult. Per §1.1, target hashes are **kind-specific**: attestation
  targets → **BLAKE2b-256** of the canonical signed body (the deployed `attestation_fingerprint`);
  delegation-cert targets → **SHA-256** of the canonical cert body (geiant `cert_hash`).
- **`held_edges`** — edge-records the verifier is **handed** and **MUST honour** against the subject,
  in **both** modes (T11, T13b).
- **`mode` + `seek`** — the same edge present only in the `ledger` (not handed) is reached by
  **`seek`** — a query for edges targeting the subject, run **only in `enforcing` mode** (see Modes &
  seek below). This is [decision 0006](../../docs/decisions/0006-enforcement-boundary-for-revocation.md)
  (accepted): held is unconditional; seek is what an enforcing consumer does and a non-enforcing one
  does not.
- **`pinned_issuer`** — the Foundation pubkey the verifier pins.

## Extended `VerifyResult` this corpus assumes
`v4` adds two signals to the v3 `{valid, reason, …}`. `expect` uses:
- **`valid`** (bool), **`reason_contains`** (substring, on rejects);
- **`lineage_status`** ∈ `{complete, truncated_unavailable, truncated_depth, anomaly_cycle}` — the §1.3
  signal. **`anomaly_cycle` (T2) and `truncated_unavailable` (T8) are distinct and a verifier MUST NOT
  collapse them** (see [#85](https://github.com/GNS-Foundation/grafomem/pull/85)); the corpus asserts
  they differ, and the runner's self-check fails if they ever match.
- **`superseded`** (bool) — set when a held `supersedes` edge targets the subject (T11): the subject is
  signature-**valid** but **not current**. Distinct from revocation (T13b), which is `valid: false`.

## Coverage matrix (→ spec clause)
| vector(s) | clause | asserts |
|---|---|---|
| `M1` | §1.1 | duplicate `{type,target}` → reject |
| `M2` | §1.1 | two `continues` → reject (≤1 predecessor) |
| `M3`,`M4` | §1.1 | multiple `supersedes`/`revokes` to distinct targets → valid |
| `H1`,`H2` | §1.1 | wrong `hash_alg` for `kind` → reject (attestation=blake2b-256, cert=sha-256) |
| `T1` | §1.3 | unrecognized type → reject |
| `T2` | §1.3 | `continues` cycle → **valid + `anomaly_cycle`** |
| `T3`,`T4` | §1.3 | `supersedes`/`revokes` cycle → reject |
| `T5` | §1.3 | mixed cycle w/ a validity-affecting edge → reject |
| `T6` | §1.3 | `continues` chain > 64 → **valid + `truncated_depth`** |
| `T7`,`T7b` | §1.3 | `supersedes` / `revokes` chain > 64 → reject |
| `T8` | §1.3 | `continues` unreachable predecessor → **valid + `truncated_unavailable`** |
| `T9`,`T10` | §1.3/§5 | agent-signed `continues` → reject; Foundation-signed → valid + `complete` |
| `T11` | §1.3 | **held** `supersedes` edge → target **valid + `superseded`** (stale, not current) |
| `T12` | §1.3 | `supersedes` unreachable target → superseder stays valid |
| `T13`,`T13b` | §1.3/§3 | **held** `revokes` edge → target refused (`valid: false`) |
| `T14` | §1.3/§3 | `revokes` unreachable target → revoker stays valid |
| `C1` | §1.1/§3 | `revokes` targeting a delegation-cert (sha-256) resolves |
| `G1`–`G7` | §2.2 | grounding **gate**: `oracle_id`/`audit_policy` required iff `dimension="grounding"` |
| `S1`,`S2` | §2.3 | unknown schema → reject; `v4` accepted |
| `B1` | §2.4 | unsigned `relates_to` → reject (signature fails) |
| `L1e`,`L1n` | §3/§4 | revoke edge in ledger (not handed): **enforcing → `valid:false` (revoked)**; **non-enforcing → valid** |
| `L2e`,`L2n` | §3/§4 | supersede edge in ledger: **enforcing → `superseded`**; **non-enforcing → valid** |
| `L3` | §3/§4 | **enforcing + seek fails → reject `undeterminable`** (Validity-Fails-Closed; distinct from `revoked`) |

## Modes & seek — 0006 (accepted 2026-09-02)
Every vector declares a **`mode`**: `enforcing` (the verifier **seeks** edges targeting the subject)
or `non-enforcing` (it does not). This is the [decision 0006](../../docs/decisions/0006-enforcement-boundary-for-revocation.md)
enforce-or-label resolution made executable — the `L*` vectors cover **both** modes, and the runner
exercises each. Seek is an injected query over the `ledger` (the reference harness scans it in-memory;
a real consumer queries its store). **`L3`** pins the failure verdict: if `seek` throws, an enforcing
verifier's revocation status is undeterminable and it **rejects** — enforcement must not silently
degrade to non-enforcement on a query error. *(The output **label** for a declared non-enforcing
consumer is NOT implemented here — it is gated on 0006's open sub-question about making the label
structurally hard to drop.)*

## Held-out, deliberately

### Grounding: the **gate**, not the **body**
`G1`–`G7` test the required-field **gate** — `oracle_id`/`audit_policy` present iff grounding-class
(`GROUNDING_DIMENSIONS = {"grounding"}`, pinned in §2.2). They do **not** test the fuller grounding
**body** (`assertion_digest`, `evidence_refs`, resolution outcomes), which lives in the internal
*Grounding-Audit Outcome delta (draft-0.2)* and is out of scope for `v4`'s verifier-behaviour surface.
**This corpus tests the gate, not the body.**

## Scope boundary — what a green run does and does NOT mean
This corpus tests **verifier verdicts**: given `(subject, ledger, held_edges, pinned_issuer)`, does the
verifier return the right `VerifyResult`? A green run means **"conformant on verifier verdicts"** — it
does **not** mean "fully conformant with the spec." The following normative MUSTs are **outside** a
v4-*verifier* corpus and are deliberately not covered here:
- **Issuer obligations** — e.g. §3's dual-write ("an *issuer* MUST emit a `revokes` edge **and** update
  the enforcement index"). About what an issuer produces, not what a verifier decides.
- **Ceremony rules** — §5.3.4 anti-fork uniqueness (≤1 *successor* per predecessor) is an
  issuer/ledger-side obligation. (Its subject-side dual, ≤1 `continues` per attestation, *is* covered —
  `M2`.)
- **Rollout process** — §2.3's expand-contract ordering ("widen `ACCEPTED_SCHEMAS` before the issuer
  emits") is a deployment sequence, not a verdict.
- **v3-verifier behaviour** — "a *v3* consumer rejects `v4` at the schema check" is about v3 verifiers;
  the v4 analogue (unknown schema → reject) is `S1`.
Verifying these needs issuer-side / process-level conformance, tracked separately.

## Running
```bash
pytest tests/test_v4_conformance.py -v          # corpus self-check runs; vectors skip (no verifier)

# run all vectors (both modes) against the reference verifier (clients/cgr-verify, JS) via the bridge:
CGR_V4_VERIFIER=conformance/cgr-attestation-v4/verify_bridge.py \
    pytest tests/test_v4_conformance.py -v
```
`CGR_V4_VERIFIER` may be an importable module name **or** a path to a `.py` file exposing
`verify(subject, ledger, pinned_issuer_hex, held_edges, mode, seek_fails) -> dict`. The bridge shells
to `clients/cgr-verify/bin/verify-v4.mjs` (needs `node`). Without `CGR_V4_VERIFIER` set, the vector
layer skips and only the **corpus self-check** (structure, signatures, T2≠T8, mode coverage) runs — so
CI stays green and the corpus can't silently rot. **The reference verifier passes all vectors in both
modes** (`39 passed` incl. the self-check).
