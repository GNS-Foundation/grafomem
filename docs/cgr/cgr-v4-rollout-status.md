# cgr.attestation.v4 — rollout status (expand-contract)

Status ledger for the `cgr.attestation.v4` rollout. The spec (`cgr-attestation-v4-spec.md` §2.3)
states the *order*; this file records *where we are*. Created 2026-09-01 because there is no
`docs/roadmap.md` and the standard's phases (P1.x) were tracked only informally in the spec.

## The shape of the rollout

Expand-contract: **verifying consumers accept `v4` and traverse (§1.3) BEFORE the issuer emits any
`v4`.** There are exactly **two verifying consumers** — the reference verifier and `@geiant/core`. The
**read surface is not a consumer**: it is issuer-side (it re-mints a fresh Foundation-signed
attestation per read; no `ACCEPTED_SCHEMAS` gate, no `relates_to` read, no traversal). Its `v4` work is
the **emission bump**, which is part of **issuance** — the last step.

## Phase ledger

| Phase | What | State |
|---|---|---|
| P1.1 | Spec (`cgr-attestation-v4-spec.md`) | done |
| P1.2 | Verifier behaviour (traversal, lineage_status, held/sought edges, modes) | done |
| P1.3 | Conformance corpus (`conformance/cgr-attestation-v4/`, 38 vectors) | done |
| P1.4 | Reference verifier (`clients/cgr-verify`) | done |
| P1.5 | Enforcing mode (injected `seek`, mode explicit+required) | done |

## Consumer phase — COMPLETE

Both verifying consumers pass **all 38 conformance vectors in both enforcing and non-enforcing modes**:

- **`@gns-foundation/cgr-verify`** (reference, JS) — runs the corpus in-repo via
  `tests/test_v4_conformance.py` + `verify_bridge.py`.
- **`@geiant/core`** (TypeScript, sibling repo) — a second independent implementation, ported
  behaviour-for-behaviour, with the corpus **vendored** (byte-identical + `_provenance` header) and an
  in-process runner. Ships **no store-backed `seek`** (absent-entirely, per
  [0007](../decisions/0007-geiant-core-has-no-reverse-edge-index.md)); enforcing mode is exercised only
  by the corpus's in-memory `seek`.

Corpus vendoring is a **single external copy** (geiant), guarded by a wellformed self-check + a sync
script. The graduation path to a published corpus artifact triggers when the vector set **freezes** or
a **second external** verifier consumer appears — neither is reached, so vendoring stands.

## Next — issuance (NOT started; gated)

The read surface's emission bump and the rest of issuance are **blocked** on the ecosystem-wide
prerequisite in [0007](../decisions/0007-geiant-core-has-no-reverse-edge-index.md): **no surface can
back enforcing-mode `seek` today** — not `@geiant/core` (no reverse index) and not grafomem's read
surface (no reverse index + metadata encrypted at rest ⇒ any `seek` is an O(tenant) decrypt-and-scan).
Issuance of validity-affecting edges (`revokes`/`supersedes`) must not begin until a reverse index
exists somewhere, or those edges bind nowhere. See the issuance-requirements list tracked with 0007.
