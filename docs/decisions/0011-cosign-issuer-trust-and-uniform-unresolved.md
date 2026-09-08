---
status:        accepted
decision_date: 2026-09-08
record_date:   2026-09-08
provenance:    found-writing-a-second-independent-verifier-against-the-corpus (gap 2 as a divergence, gap 1 as an agreement that was luck)
scope:         cgr.cosign.v1 (§8) — PROPOSED spec, amended after its first two verifier implementations
---

# 0011 — `cgr.cosign.v1` MUST pin the issuer key, and unresolvable is uniform across operators

## Context

`cgr.cosign.v1` was implemented twice, independently, against the one conformance corpus: a JS
reference verifier (`clients/cgr-verify/src/cosign.js`, PR #128) written from §8, then a Python
verifier (`packages/grafomem-cgr/src/grafomem_cgr/cosign_verify.py`, PR #129) written **from the spec,
not transliterated** from the JS — two implementations are only worth their cost if arrived at
separately, and a port tests nothing about the spec.

Two implementations against one corpus is a spec-finding instrument, not just a correctness check. It
found two gaps of the **same shape** — something the spec assumed and never stated — surfaced through
opposite signals:

- **Gap 2 surfaced as a divergence.** The two verifiers returned different verdicts on an off-corpus
  input, which points straight at underspecification.
- **Gap 1 surfaced as an agreement that was luck rather than specification** — the more useful finding,
  because agreement normally reads as *confirmation*. Both verifiers pin nothing about the issuer; they
  agreed only because neither had a reason to differ. An unstated assumption that every implementation
  happens to share is invisible to a corpus **and** to cross-implementation agreement; it takes reading
  *why* they agree to see it.

## The findings

### Gap 1 — the issuer key is not pinned (the larger one)

No clause in §8 requires `system_metadata.issuer_key_id` to match a trusted key. Step 6 recomputes
`JCS(record minus envelope keys)` and verifies `system_signature` **against the key the record names
itself**. So both verifiers check the system signature against the record's **self-declared**
`issuer_key_id`, and a cosign record therefore verifies as **valid under any issuer key that signs it
consistently** — the record vouches for its own issuer.

**The consequence, stated plainly:** the approver binding holds; the issuer binding does not. For an
envelope whose entire purpose is attributing a decision to **two** parties, that attributes **one** of
them. §5.1's own principle — a validity-affecting fact must be verifiable from the record — is not met
for the issuer: *which system issued this* is validity-affecting, and it is currently taken on the
record's own word.

Attestation v3/v4 does not have this hole: it pins `issuer_key_id === pinned` and rejects otherwise
(`cgr-ticket-05-identity-binding.md` Task E, "Keep `issuer_key_id === pinned` … exactly as they are";
`read-surface.md`: "verify it yourself against a **pinned** issuer key"). §8 was written without that
step and neither implementation added one, because the spec did not ask for it.

### Gap 2 — unresolvable is not uniform across operators

Decision [0010](0010-cosign-predicate-unresolved-must-reject.md) made an **unresolvable** required-ness
predicate REJECT with `predicate_unresolved`, but it pinned that for **field resolution** (absent /
non-scalar / null path) and **ordering-operand type** (a type-mismatched comparison) — not for the
**`in`-operand shape**. So §5.1 `in` with a **non-array** `value` is read two ways:

- **JS** — `value` is not iterable ⇒ membership is false ⇒ predicate **false** ⇒ approver **optional** ⇒
  record can **pass** unsigned.
- **Python** — the operand cannot be meaningfully evaluated ⇒ **reject** `predicate_unresolved`.

Both are defensible readings of the un-amended text; **one is a fail-open**, and it is the *exact* class
0010 closed — an unevaluable predicate silently resolving to "approval not owed" — reappearing one
operator over because 0010 enumerated the cases it had in front of it rather than stating the general
rule. No corpus vector exercises `in`, so this was never a vector disagreement; it was found by the two
verifiers disagreeing on an input the corpus did not contain.

## Decision

Both gaps are the same shape and land in one record.

### Amendment 1 — §8 MUST pin the issuer key against a required trusted set

§8 gains a normative step: the verifier MUST check `system_metadata.issuer_key_id` against a
**caller-supplied trusted issuer set**, and MUST reject (reason `issuer_untrusted`) if the record's
issuer key is not in that set. **Self-declaration is insufficient** — verifying the system signature
against the key the record names proves only internal consistency, not that a trusted system issued it.

Following attestation v3's precedent for the interface shape, with **one hard rule that goes beyond it**:

- The trusted issuer set is a **REQUIRED caller input with no default.** There is **no
  trust-everything path** — if the caller supplies no trusted set, the verifier MUST reject (or refuse
  to run), never fall back to accepting the self-declared key. A permissive default is how this gap got
  here; the spec closes it by making the safe input mandatory rather than optional.
- The check is ordered **before** the system-signature verification it qualifies (an untrusted issuer is
  rejected before spending a signature verification on it), and it is a **gate**, not a surfaced field —
  unlike the *assurance tier* (§8, surfaced-not-gated), issuer trust is a hard verifier obligation, the
  same standing as schema and profile resolution.

This is the issuer-side analogue of ticket-05's subject-key binding: there the *subject* key had to be
pinned to the manifest identity; here the *issuer* key must be pinned to a trusted set.

### Amendment 2 — unresolvable is uniform across all operators

Extend 0010's principle **as a general rule, not an operator enumeration**: *any* required-ness
predicate that **cannot be evaluated for any reason** — including a **malformed operand of any
operator** (a non-array `in` value, and anything of the same kind for operators added later) — is
**UNDETERMINED** and the verifier MUST reject with reason `predicate_unresolved`. Stated generally so
the next operator added to §5.1 inherits it without a further amendment. 0010 remains the origin of the
principle; this generalises its **scope** from the enumerated cases (field resolution, ordering-operand
type) to "unevaluable for any reason."

## Consequences

- **Gap 1 is a behaviour change for every existing verifier**, JS and Python alike — neither pins the
  issuer today, so both must gain the step and the required trusted-set input. This is not aligning one
  implementation to the other (as gap 2 is); it is a step both were missing. The `[verify]` surface and
  the CLI bridge grow a required trusted-issuer parameter with no default.
- **Gap 2 aligns JS to Python.** The Python reading was already correct under 0010's *principle*; the JS
  reading was correct under 0010's *letter*. The amendment makes the letter match the principle, and JS
  changes to reject.
- **Cost, stated plainly.** Amendment 1 makes the trusted set a mandatory input: a caller that does not
  supply one gets nothing verified, by design — there is no silent accept. This is the same
  friction-until-configured trade 0010 accepted (visible, one-directional, bounded), applied to issuer
  trust: a verifier with no trusted set refuses, it does not trust-everything. Amendment 2 costs
  nothing new beyond 0010's already-accepted DoS-until-fixed for a malformed predicate, now covering one
  more malformed shape.
- **The principle, both times.** *A validity-affecting fact must be verifiable from the record, against
  something the relying party already trusts.* Gap 1: which system issued this is validity-affecting and
  must be checked against a trusted set, not the record's own claim. Gap 2: whether approval was owed is
  validity-affecting, and if the predicate deciding it cannot be evaluated — for **any** reason — the
  record's validity is unknown and it must not pass.

## Corpus (next PR, separate)

Per the discipline, the amendment lands first; the corpus and the two verifier changes follow in their
own PRs. The corpus gains:

- **An untrusted-issuer vector.** The vectors already carry `pinned_issuer`; the harness wires the
  existing per-vector `pinned_issuer` (or a trusted set derived from it) into the `verify` call rather
  than inventing a parallel fixture concept — the corpus stays the constraint.
  - **Non-vacuity probe, named:** the untrusted-issuer vector MUST be signed **correctly** by the
    untrusted key (a third deterministic test key, `0x33`, so the corpus stays reproducible). It
    verifies **cleanly** under its self-declared `issuer_key_id`, so any verifier that skips the trust
    check passes it — that is the N1-style property. Without it the vector proves nothing: same failure
    mode as an N-vector that re-signs over its own stripped body, or a corpus that checks a defect that
    an earlier ordered step already catches.
- **Two `in` vectors** — an `in` whose array **holds** (member present ⇒ predicate true) and an `in`
  whose array **does not hold** (member absent ⇒ predicate false) — so the *well-formed* `in` path is
  pinned in both directions, alongside the non-array `in` that now rejects `predicate_unresolved`.

## Open questions

- The **shape of the trusted issuer set** at the interface (a set of hex key ids, vs. a richer
  descriptor carrying assurance metadata) is left to the verifier libraries, so long as the "required,
  no default, no trust-everything path" rule holds. Non-normative.
- Whether `issuer_untrusted` should carry the offending `issuer_key_id` in the reason for operability
  (proposed: yes, mirroring 0010's field-path recommendation). Non-normative; left to the verifier.
- 0010's open questions stand; this record resolves **only** issuer pinning (gap 1) and the uniformity
  of unresolvable across operators (gap 2).
