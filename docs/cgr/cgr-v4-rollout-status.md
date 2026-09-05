# cgr.attestation.v4 — rollout status (expand-contract)

Status ledger for the `cgr.attestation.v4` rollout. The spec (`cgr-attestation-v4-spec.md` §2.3)
states the *order*; this file records *where we are*.

**Provenance.** Created 2026-09-01 because there is no `docs/roadmap.md`, and the standard's phases
(P1.x) were tracked only informally in the spec. Notably, the spec's §7 cites "P1.3 **per the
roadmap**" as though a roadmap file existed — it never did. This document is that file, created after
the fact to stop the spec referencing a phantom. (`ops/ROADMAP.md` exists but is a Phase-0
Ulissy-dogfooding limitations log — not the CGR-standard rollout, so it is the wrong home.)

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

## What issuance requires (NOT started)

Issuance = the read surface (and the `/v1/cgr/attestation` router) emitting `cgr.attestation.v4`, and
turning on validity-affecting edges. Nothing below is started; this is the list.

Two things decouple from the rest and deserve to be read first:

> ### The `continues` carve-out — a shippable path that does NOT wait on the reverse index
>
> `continues`-only issuance is **not** gated by the reverse index (blocker 1). `continues`
> **degrades** (Lineage-Degrades), it does not enforce — no consumer has to `seek` it — so linking a
> lineage predecessor needs no queryable index. This **decouples rotation continuity from revocation
> enforcement**: concretely, the orphaned `c14094ea…` chain could be linked to `d3caa6f1…` via a
> `continues` edge **before any reverse index exists**. Since [0004](../decisions/0004-no-identity-continuity-across-rotation.md)
> (no identity-continuity across rotation) and [0005](../decisions/0005-custody-managed-principals.md)
> (custody-managed principals) were about exactly this gap, `continues`-only issuance is a **shippable
> path now**, independent of the hard blocker. (It still requires the emission-bump mechanics below and
> — like all v4 emission — a published v4-capable verifier; see the next callout. It does **not**
> require blocker 1 or the `revokes`/`supersedes` machinery.)

> ### Publish the v4-capable reference verifier (item 8) — expand-contract is not done until it ships
>
> External consumers verify with the **published** `@gns-foundation/cgr-verify`, not the in-repo
> source. In-repo green **does not reach past the repo boundary**: an external tenant on a v3-capable
> published version will **reject** every `v4` attestation at the schema check. So a published,
> v4-capable `@gns-foundation/cgr-verify` is a hard precondition for **any** v4 emission — including the
> `continues`-only path above — and it is **independent of the reverse index**. Expand-contract was
> **incomplete** until it was published — **now done** (item 8 below): `@gns-foundation/cgr-verify@0.2.0`
> is live on npm and consumer-verified from a clean install.

### Hard blockers

1. **A reverse index** (the [0007](../decisions/0007-geiant-core-has-no-reverse-edge-index.md)
   prerequisite) — some surface must answer `seek(fp) → {revokes/supersedes edge-records}` as an
   *indexed* query; none can today. Requires the 0007 Q1 design choice (geiant-local edge table + write
   path, or grafomem enforcement-index over HTTP). On the grafomem side, the metadata-encryption
   barrier forces a **plaintext, indexable target-fingerprint column or a dedicated edge table** — you
   cannot GIN-index encrypted `metadata`. **Gates only `revokes`/`supersedes` emission; `continues`-only
   is not gated by this (see carve-out above).**
2. **The 0006 Foundation decision to proceed** — [0007](../decisions/0007-geiant-core-has-no-reverse-edge-index.md)
   Q2: validity-affecting issuance MUST NOT begin until (1) exists. Plus [0006](../decisions/0006-enforcement-boundary-for-revocation.md)'s
   still-open **label non-strippability** sub-question, which gates any enforcement-label field an
   emitted attestation would carry.

### The emission bump

3. **`attestation.py` v3 → v4** — stamp `cgr.attestation.v4`; add the signed fields `domain`,
   `verifiability_tag`, `decision_date`, `recorded_at`, `backfilled`; grounding fields
   (`oracle_id`/`audit_policy`/`n_unresolvable`) present **iff** `dimension == grounding` and absent
   otherwise (or consumers reject at the grounding gate).
4. **`relates_to` construction from the substrate** — map real events to edges: rotations
   (`prev_key`/`new_key`) → `continues`; revocation/supersession events → `revokes`/`supersedes`, with
   correct per-kind `hash_alg` and fingerprints, obeying the multiplicity rules. The substrate must
   actually *carry* the relation data — non-trivial.

### Operational reality of the flip — downtime & rollback cost (corrected 2026-09-04)

The emission bump reasoning leaned on "the read surface re-mints per request and persists nothing, so
the flip is a single-constant change and **rollback is immediate**." That is true of the **data** —
there is no stored v4 body to reconcile — but it is **false of the service**, and the distinction was
discovered the hard way during the 2026-09-04 flip:

- The flip is a **redeploy**, and prod runs a **single replica with no healthcheck-gated handoff**. The
  2026-09-04 flip left prod **externally unreachable (`HTTP 000`) for ~25–30 minutes** during the
  cutover (old instance stops before the slow-cold-starting new one serves). Any redeploy does this;
  it is not caused by the schema change.
- Therefore **rollback is NOT immediate**: reverting the constant is *another* redeploy = **another
  ~25–30 minutes of downtime**. An abort (bad deploy out **and** revert in) is **~an hour of
  unavailability**, not a moment. Budget deploys and rollbacks as ~30-min planned outages until a
  zero-downtime path exists (second replica + rolling handoff, or moving startup warmup off the
  readiness path). Tracked as an operational-resilience (DORA) issue, not an inconvenience — see
  grafomem#108.

### Tests & fixtures

5. **The v3-pinned read-surface tests** — `test_cgr_read_surface.py:100` and `test_cgr_read_mcp.py:196`
   both assert `schema == "cgr.attestation.v3"`; update/parameterize to v4, add assertions for the new
   fields + `relates_to`, and preserve the load-bearing invariants (honest-scope, REST↔MCP equivalence,
   no-default-score).
6. **A new v4 golden fixture** (spec §7: the v3 golden MUST NOT be mutated) + an **emit-conformance
   round-trip** test: emitted v4 must verify under the reference verifier.
7. **DB migration** for the reverse index from (1).

### Ecosystem readiness (expand-contract completion)

8. **✅ DONE — Publish the v4-capable `@gns-foundation/cgr-verify`** so *external* tenant consumers can
   verify v4 before emission (in-repo-green isn't enough; see callout above). **Consumer-verified from a
   clean directory outside the repo at each publish** (`npm install` → the installed package's own
   `npm test` runs both suites, and `verifyCGRAttestationV4` is importable/callable *by package name*).
   Published history (kept, not overwritten):
   - **`0.2.0` — 2026-09-02** — initial v4 support. Tarball sha
     `7b75f5267e516d9259698a70c6dedd9a5eeae149` (9 files / 13.6 kB); 18 v3 golden + 10 v4 smoke.
   - **`0.3.0` — 2026-09-03** — **evidence_tier-aware (§1.1)**. Tarball sha
     `28353705f7ab5ce69d179cbbdce0f28e8f56dbb7`; 18 v3 golden + 13 v4 smoke. Consumer check confirmed
     `evidence_tier` **surfaces through the public import** and a missing tier rejects. **The published
     reference verifier is now evidence_tier-aware** — external consumers can verify the new field
     without trusting any server.

   The requirement text is kept above the status on purpose — what was needed matters as much as that
   it's done.
9. **Confirm no other consumer schema-gates v4** — cloud-v2 is display-only (renders as-attested) so it
   should tolerate v4, but verify it doesn't reject/mis-parse the new fields before flipping emission.
