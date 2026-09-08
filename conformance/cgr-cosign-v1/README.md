# `cgr.cosign.v1` conformance corpus

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  PROFILE RESOLUTION (§8 step 2) IS NORMATIVE, AND THE PROFILE REGISTRY          ║
║  DOCUMENT IS UNWRITTEN (§11 Q3). registry.json IN THIS DIRECTORY IS A TEST      ║
║  FIXTURE — the corpus went first. Whoever writes the real registry MUST either  ║
║  match these pinned entries (bound-unconditional, bound-predicate, free, and    ║
║  the unknown-profile reject behaviour) or RETARGET every vector in this corpus. ║
║  Do not write a "reasonable" registry in ignorance of this file; the corpus is  ║
║  the constraint. (The same warning rides inside registry.json — files travel    ║
║  without their READMEs.)                                                        ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

The executable, language-neutral encoding of the **normative MUST rules** of
[`docs/cgr/cgr-cosign-v1-spec.md`](../../docs/cgr/cgr-cosign-v1-spec.md), **as amended by
decisions [0010](../../docs/decisions/0010-cosign-predicate-unresolved-must-reject.md)**
(predicate-unresolved → REJECT) **and
[0011](../../docs/decisions/0011-cosign-issuer-trust-and-uniform-unresolved.md)** (issuer key
MUST be pinned against a trusted set; unresolvable is uniform across operators). A cosign
verifier is **conformant iff** it returns each vector's `expect`. Written **spec → corpus →
implementation** — the order `v4` followed, and the order that let `v4`'s corpus catch a spec
error. Here it caught the §5.1 fail-open **before** any verifier existed (decision 0010); and
decision 0011's two gaps were caught by **two independently-written verifiers against this
corpus** (gap 2 as a divergence, gap 1 as an agreement that was luck), then encoded here.

**The trusted issuer set is part of the interface contract (0011 §8.2a).** `verify()` takes a
**REQUIRED** trusted-issuer-set argument with **no default and no trust-everything path**. The
harness derives it per-vector from each vector's `pinned_issuer` — the trusted issuer for every
vector is the `0x11` key it pins, **except `I2`** (the control) which pins `0x33`. A real
deployment supplies the trusted set at the call site; it is **never** read from the record's
self-declared `issuer_key_id`.

## Files
- **`vectors.json`** — the corpus (generated). Each vector: `{id, clause, spec_lines,
  title, pinned_issuer, approver_pub, subject, expect, [ledger], [tags]}`.
- **`generate.py`** — deterministic generator (repeating-byte **test** issuer key `0x11`
  / approver key `0x22` / **untrusted issuer `0x33`** — **not real keys**). Reuses the
  production JCS canonicalizer (`rfc8785`), Ed25519-over-JCS **no prehash**, **BLAKE2b-256**
  content digests, and the real `DOMAIN_TAG` `grafomem.hitl.approval.v1:` — so vector bytes
  match a real verifier. It **proves the nesting invariant and the issuer-pinning invariant
  on the bytes** at generation time (see below).
  Regenerate: `python3 conformance/cgr-cosign-v1/generate.py`.
- **`registry.json`** — the **pinned test profile registry** (fixture; see the boxed
  warning). Also carries the **trusted-issuer-set interface contract** (`_WARNING_TRUSTED_ISSUERS`).
- **`issuer.json`** — the pinned test pubkeys (incl. the untrusted `0x33`) + domain tag.
- **`verify_bridge.py`** — the `verify(record, registry, ledger, trusted_issuers)` contract a
  verifier implements (the 4th arg is the 0011 trusted set). The two reference verifiers
  (`clients/cgr-verify`, `packages/grafomem-cgr`) implement it; the bridge for each is wired
  in the verifiers PR that follows this corpus.
- Runner: [`tests/test_cosign_conformance.py`](../../tests/test_cosign_conformance.py).

## Layer split (§8)
- **Layer 1 — `test_corpus_wellformed`, always runs (no verifier):** structure, family
  coverage, that valid-expected records genuinely verify (both signatures — the system
  signature under the record's **own self-declared issuer** — plus the content digest), the
  **byte-level nesting invariant** (N1/N2), the K2 lift (digest ≠ recompute), the
  decision-0010 U-vectors, the decision-0011 `in` vectors (U4/R5/R6), and the **issuer-pinning
  byte probe** (I1/I2). Keeps the corpus PR green and un-rottable before a verifier exists.
- **Layer 2 — `test_cosign_conformance`, skips unless `CGR_COSIGN_VERIFIER` is set:**
  runs each vector's `expect` against a verifier. A green run ⟺ "conformant on §8 verifier
  verdicts" — **not** "fully spec-conformant" (see scope boundary).

## Coverage matrix (→ spec clause)
| vector(s) | clause | asserts |
|---|---|---|
| `W1` | §1/§8 | valid bound, approve, two nested signatures → valid |
| `W2` | §5 | valid free, references by `b2-256:` hash in `content_body` → valid |
| `S1` | §8.1 | schema ≠ `cgr.cosign.v1` → reject |
| `P1` | §8.2 | unknown profile → reject (fail closed) |
| `P2` | §8.2 | `approval_mode` ≠ profile's mode → reject |
| `D1` | §8.3 | `content_digest` mismatch → reject **before** predicate eval (ordering) |
| `R1` | §8.4 | unconditional REQUIRED, approver absent → reject |
| `R2` | §5.1/§8.4 | predicate holds (`risk_class=high`), approver absent → reject |
| `R3` | §5.1/§8.3a | predicate false (`risk_class=low`), approver absent → **pass** |
| `R4` | §8.4 | approver present (optional) but **invalid** → reject (present MUST verify) |
| `U1`–`U3` | §5.1/§8.3a | predicate field **absent / non-scalar / null** → **reject `predicate_unresolved`** (decision 0010) |
| `U4` | §5.1/§8.3a | `in` operator with a **non-array `value`** → **reject `predicate_unresolved`** (decision **0011**: unresolvable is uniform across operators; high-risk **unsigned** record, so a verifier reading non-array `in` as predicate-false fails open) |
| `R5` | §5.1/§8.4 | **well-formed `in`** predicate **holds** (member present) → approver REQUIRED, signed → valid |
| `R6` | §5.1/§8.3a | **well-formed `in`** predicate **false** (member absent) → approver OPTIONAL, unsigned → valid |
| `N1` | §7.1/§8.6 | strip approver sig **under an optional profile** → §8.4 passes, **§8.6 system sig fails** → reject (nesting isolated) |
| `N2` | §7.1/§8.6 | **tamper a signed field** (`recorded_at`) after signing → §8.6 system sig fails → reject |
| `K1` | §4/§8.7 | duplicate `(approver_key_id, record_nonce)` in `ledger.seen` → reject (replay) |
| `K2` | §4/§8.3 | approver sig lifted onto different content → `content_digest` mismatch → reject |
| `A1` | §6/§8.8 | `approve` with `agent_draft_digest` present → reject |
| `A2` | §6/§8.8 | `modify` without `agent_draft_digest` → reject |
| `A3` | §6/§8.8 | `override` with `agent_draft_digest` → valid (act surfaced) |
| `F1` | §8.9 | free-mode malformed reference hash → reject |
| `F2` | §8.9 | free-mode well-formed but unresolvable reference → **degrade** (surface, valid) — **tagged `flips-if-8.9-must-resolve`** |
| `X1` | §8 | verifier **surfaces** `approver_id`/`approver_act`/`decision_date` and **MUST NOT gate** → valid + surfaced |
| `I1` | §8.2a | system signature **valid** under a self-declared issuer **not in the trusted set** → **reject `issuer_untrusted`** (decision **0011** gap 1) |
| `I2` | §8.2a | **the same record** with its issuer **in** the trusted set → valid (**positive control**: proves `I1` rejects on the trust check, not on any other defect) |

## Corpus correction — found implementing the JS verifier
The original `N1`/`N2` (shipped in the corpus PR) used a **REQUIRED** profile: stripping/altering
the approver signature rejected at **§8.4** (approver required-and-absent / present-and-invalid)
**before** the system-signature check (§8.6), so they did **not** isolate nesting — a non-conformant
**parallel-signature** verifier would have rejected them too (identical to `R1`/`R4`). Building the
reference verifier surfaced this (the same way `v4`'s corpus caught its malformed-hash placeholder).
Fixed here so the rejection is forced through §8.6: `N1` strips under an **optional** profile; `N2`
tampers a signed non-content field. Both now reject with `system signature`, and only a **nested**
verifier passes them.

## The nesting invariant, proved on the bytes (N1)
`generate.py:prove_invariants()` and `test_corpus_wellformed` both assert: the **original**
system signature (from the intact record) **fails** when verified against the **stripped**
body — signature from the intact record, body from the stripped variant. This is the
property (§3.3/§7.1) — *not* "a signature recomputed over the stripped body" (which would
prove nothing). A control confirms the intact signature verifies against the intact body.

## The issuer-pinning invariant, proved on the bytes (I1) — decision 0011
The same non-vacuity discipline for gap 1. `prove_invariants()` and `test_corpus_wellformed`
both assert that `I1`'s system signature **verifies cleanly under its own self-declared `0x33`
issuer key** — so a verifier that **skips** step 2a (issuer pinning) **passes** `I1`. The record
is otherwise-valid; the **only** defect is that its self-declared issuer (`0x33`) is **not** the
pinned/trusted one (`0x11`). Without that clean self-verify the vector would reject on crypto,
not on the trust check, and prove nothing — the same failure mode as recompute-over-stripped
(N1) or a defect an earlier ordered step already catches (the original N1/N2). `I2` is the
**positive control**: byte-identical to `I1` but with `0x33` **in** the trusted set (it pins
`0x33`), so it verifies valid — which is what proves `I1`'s rejection is attributable to the
trust check and nothing else.

## Positions this corpus takes (named, not hidden)
- **Content digest = BLAKE2b-256** (§2.1 `[FLAG]`, chosen).
- **`agent_draft_digest` absent for `approve`** (§6 `[FLAG]`, chosen).
- **§8.9 free-mode = degrade** (surface `references_unresolved`, do not reject) — the spec's
  proposed default; `F2` is tagged so it flips cleanly if §8.9 resolves to MUST-resolve.
- **Free-mode references live in `content_body`, never `evidence_ref`** (§5 note added in the
  decision-0010 amendment).
- **Issuer trust is a REQUIRED caller input, no default, no trust-everything path** (§8.2a,
  decision 0011). The trusted set is derived per-vector from `pinned_issuer`; a verifier given
  no trusted set MUST reject, never fall back to the record's self-declared issuer.
- **Unresolvable is uniform across operators** (§5.1/§8.3a, decision 0011): a non-array `in`
  value (`U4`) rejects `predicate_unresolved`, exactly as an absent/non-scalar/null field does.

## Scope boundary — what a green run does and does NOT mean
Tests **verifier verdicts**. Issuer **trust-set membership** (pinning, §8.2a) is now **in**
scope — `I1`/`I2` are verdicts. Still held out (issuer-side / ceremony / assurance, like
`v4`): issuer key **custody** and the `gns_browser` signing ceremony (§3, §9), and **how a
deployment populates its trusted set** (the corpus pins *that a verifier MUST check against
one*, not *how the set is built*); `record_nonce` **freshness-as-challenge** (§4 — only
uniqueness is a verdict); **compromised-issuer re-mint detection** (§7.3 — needs the gcrumbs
chain + external anchoring, not a single record); the **approver-identity assurance layer /
gap 3a** (§9/§10).

## Running
```bash
pytest tests/test_cosign_conformance.py -v          # Layer-1 self-check runs; vectors skip (no verifier)

# when a verifier exists:
CGR_COSIGN_VERIFIER=conformance/cgr-cosign-v1/verify_bridge.py \
    pytest tests/test_cosign_conformance.py -v
```
Without `CGR_COSIGN_VERIFIER`, the vector layer skips and only the **corpus self-check**
runs — so CI stays green and the corpus can't silently rot.
