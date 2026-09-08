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
decision [0010](../../docs/decisions/0010-cosign-predicate-unresolved-must-reject.md)**
(predicate-unresolved → REJECT). A cosign verifier is **conformant iff** it returns each
vector's `expect`. Written **spec → corpus → implementation** — the order `v4` followed,
and the order that let `v4`'s corpus catch a spec error. Here it caught the §5.1
fail-open **before** any verifier existed (decision 0010).

## Files
- **`vectors.json`** — the corpus (generated). Each vector: `{id, clause, spec_lines,
  title, pinned_issuer, approver_pub, subject, expect, [ledger], [tags]}`.
- **`generate.py`** — deterministic generator (repeating-byte **test** issuer key `0x11`
  / approver key `0x22` — **not real keys**). Reuses the production JCS canonicalizer
  (`rfc8785`), Ed25519-over-JCS **no prehash**, **BLAKE2b-256** content digests, and the
  real `DOMAIN_TAG` `grafomem.hitl.approval.v1:` — so vector bytes match a real verifier.
  It **proves the nesting invariant on the bytes** at generation time (see below).
  Regenerate: `python3 conformance/cgr-cosign-v1/generate.py`.
- **`registry.json`** — the **pinned test profile registry** (fixture; see the boxed
  warning).
- **`issuer.json`** — the pinned test pubkeys + domain tag.
- **`verify_bridge.py`** — the `verify(record, registry, ledger)` contract a future
  verifier implements. **No cosign verifier exists yet**; the bridge documents the
  interface and Layer 2 skips.
- Runner: [`tests/test_cosign_conformance.py`](../../tests/test_cosign_conformance.py).

## Layer split (§8)
- **Layer 1 — `test_corpus_wellformed`, always runs (no verifier):** structure, family
  coverage, that valid-expected records genuinely verify (both signatures + content
  digest), the **byte-level nesting invariant** (N1/N2), the K2 lift (digest ≠ recompute),
  and the decision-0010 U-vectors. Keeps the corpus PR green and un-rottable before a
  verifier exists.
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

## Positions this corpus takes (named, not hidden)
- **Content digest = BLAKE2b-256** (§2.1 `[FLAG]`, chosen).
- **`agent_draft_digest` absent for `approve`** (§6 `[FLAG]`, chosen).
- **§8.9 free-mode = degrade** (surface `references_unresolved`, do not reject) — the spec's
  proposed default; `F2` is tagged so it flips cleanly if §8.9 resolves to MUST-resolve.
- **Free-mode references live in `content_body`, never `evidence_ref`** (§5 note added in the
  decision-0010 amendment).

## Scope boundary — what a green run does and does NOT mean
Tests **verifier verdicts**. Held out (issuer-side / ceremony / assurance, like `v4`):
issuer key custody and the `gns_browser` signing ceremony (§3, §9); `record_nonce`
**freshness-as-challenge** (§4 — only uniqueness is a verdict); **compromised-issuer
re-mint detection** (§7.3 — needs the gcrumbs chain + external anchoring, not a single
record); the **approver-identity assurance layer / gap 3a** (§9/§10).

## Running
```bash
pytest tests/test_cosign_conformance.py -v          # Layer-1 self-check runs; vectors skip (no verifier)

# when a verifier exists:
CGR_COSIGN_VERIFIER=conformance/cgr-cosign-v1/verify_bridge.py \
    pytest tests/test_cosign_conformance.py -v
```
Without `CGR_COSIGN_VERIFIER`, the vector layer skips and only the **corpus self-check**
runs — so CI stays green and the corpus can't silently rot.
