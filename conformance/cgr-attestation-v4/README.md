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
- Runner: [`tests/test_v4_conformance.py`](../../tests/test_v4_conformance.py).

## Resolution model
A verifier is given the **`subject`** attestation and a **`ledger`** — a map from `target.hash` to the
attestation/cert it resolves — plus **`pinned_issuer`** (the Foundation pubkey). It follows
`relates_to` edges through the ledger. Per §1.1, target hashes are **kind-specific**:
attestation targets → **BLAKE2b-256** of the canonical signed body (the deployed
`attestation_fingerprint`); delegation-cert targets → **SHA-256** of the canonical cert body (geiant
`cert_hash`).

## Extended `VerifyResult` this corpus assumes
`v4` adds a lineage signal to the v3 `{valid, reason, …}`. `expect` uses:
- **`valid`** (bool), **`reason_contains`** (substring, on rejects);
- **`lineage_status`** ∈ `{complete, truncated_unavailable, truncated_depth, anomaly_cycle}` — the §1.3
  signal. **`anomaly_cycle` (T2) and `truncated_unavailable` (T8) are distinct and a verifier MUST NOT
  collapse them** (see [#85](https://github.com/GNS-Foundation/grafomem/pull/85)); the corpus asserts
  they differ, and the runner's self-check fails if they ever match.

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
| `T7` | §1.3 | `supersedes` chain > 64 → reject |
| `T8` | §1.3 | `continues` unreachable predecessor → **valid + `truncated_unavailable`** |
| `T9`,`T10` | §1.3/§5 | agent-signed `continues` → reject; Foundation-signed → valid + `complete` |
| `T13`,`T13b` | §1.3/§3 | a held `revokes` edge refuses its target |
| `C1` | §1.1/§3 | `revokes` targeting a delegation-cert (sha-256) resolves |
| `G1`–`G7` | §2.2 | grounding **gate**: `oracle_id`/`audit_policy` required iff `dimension="grounding"` |
| `S1`,`S2` | §2.3 | unknown schema → reject; `v4` accepted |
| `B1` | §2.4 | unsigned `relates_to` → reject (signature fails) |
| `L1`,`L2` | §3/§4 | **`pending-0006B`** — see below |

## Held-out, deliberately

### `pending-0006B` — verdicts that flip on 0006 Question B
`L1` (revoke-liveness) and `L2` (supersede-liveness) present a subject that is valid on its own while a
`revokes`/`supersedes` edge targeting it exists **in the ledger but is not handed to the verifier**.
Whether the verifier **MUST seek** that edge before trusting is
[decision 0006](../../docs/decisions/0006-enforcement-boundary-for-revocation.md) **Question B**
(uniform vs scoped enforcement), still open. Their `expect` is **`null`**, with a `flips_on` note —
not a guessed verdict. This is what §4 of the spec was for: **0006 Question B is now a concrete list
of vectors whose verdicts flip on the answer.** When 0006-B is decided, set their verdicts and drop
the `pending` marker.

### Grounding: the **gate**, not the **body**
`G1`–`G7` test the required-field **gate** — `oracle_id`/`audit_policy` present iff grounding-class
(`GROUNDING_DIMENSIONS = {"grounding"}`, pinned in §2.2). They do **not** test the fuller grounding
**body** (`assertion_digest`, `evidence_refs`, resolution outcomes), which lives in the internal
*Grounding-Audit Outcome delta (draft-0.2)* and is out of scope for `v4`'s verifier-behaviour surface.
**This corpus tests the gate, not the body.**

## Running
```bash
pytest tests/test_v4_conformance.py -v          # corpus self-check runs; vectors skip (no verifier)
CGR_V4_VERIFIER=<module> pytest tests/test_v4_conformance.py -v   # run vectors against a v4 verifier
```
The verifier module must expose `verify(subject, ledger, pinned_issuer_hex) -> dict`. Until P1.4 wires
one, the vector layer skips and the **corpus self-check** (structure, signatures, T2≠T8, pending shape)
runs — so the corpus can't silently rot.
