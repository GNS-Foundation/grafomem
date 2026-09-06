# CGR co-signature envelope `cgr.cosign.v1` — specification (PROPOSED)

- **Status:** **Proposed** — design document, authored against the **accepted** decision in
  `docs/decisions/0009-standard-expresses-one-actor-approval-needs-two.md` (§ "Decision — the two-party
  co-signature envelope", accepted 2026-09-06). Not implementation; no corpus, no fixtures, no verifier
  code in this document.
- **Schema string:** `cgr.cosign.v1`
- **Date:** 2026-09-06 · **Amended 2026-09-06** — added **predicate-based required-ness** (§5.1). Found
  by the first implementation attempt (mapping the envelope onto CCR's learning transaction): §5's
  per-profile *static* required-ness could not express doc 04's `HighRiskUpdate ⇒ RequiredApproval`, so
  the spec is catching up to a real consumer. Recorded visibly, per the corrections discipline.
- **Inputs:** [0009](../decisions/0009-standard-expresses-one-actor-approval-needs-two.md) (the accepted
  decision this specifies), [0006](../decisions/0006-enforcement-boundary-for-revocation.md) (enforce-or-label;
  the strippability register), [0003](../decisions/0003-principal-identity-is-not-stable.md) (why the
  approver key needs a separate assurance layer), and `cgr-attestation-v4-spec.md` §0/§2 (the
  canonicalization and signature conventions reused here).
- **Audience:** the **verifying consumers** that implement §8 against this, and the **issuers** that
  emit it (the capture path; any profile author). A profile (e.g. `cgr.disposition.v1`) is specified
  separately and **references** this envelope; this document specifies the envelope only.

This document uses **MUST / SHOULD / MAY** per RFC 2119. Two marker conventions appear:

- **`[OPEN]`** — a question this spec deliberately does **not** resolve; flagged, not hidden.
- **`[FLAG]`** — a call that could reasonably go the other way; the reasoning is stated so a reviewer
  can overturn it deliberately.

---

## 0. Framing — what `cgr.cosign.v1` is

`cgr.cosign.v1` is a **general, record-agnostic two-party co-signature envelope**: a wrapper that binds
**one human approver's signature** and **one system signature** to an arbitrary content body, such that
the approval is **independently verifiable** and **non-strippable within conformance**. It is the
primitive whose absence [0009](../decisions/0009-standard-expresses-one-actor-approval-needs-two.md)
records — four independently-built systems recorded human approval as *unsigned metadata inside a
system-signed container*.

**Why an envelope, not a `v4` field and not a per-record schema** (from the accepted 0009): `v4` cannot
host a two-actor record (no record class fits, one signer slot, no content-field home), and a per-record
schema would re-implement this primitive for each payload that needs it. The envelope is defined once;
**payloads are its profiles.** `cgr.disposition.v1` (the B2 AML disposition) is the first profile;
CCR's learning transaction and any MCP tool-approval are further profiles.

**Carried over from `v4`, unchanged:** canonicalization is **RFC 8785 (JCS)**; signatures are **Ed25519
over the JCS-canonical bytes, with no SHA-512 prehash**; **envelope keys** (defined in §2.3) are
excluded from the signed body. `cgr.cosign.v1` reuses these so a verifier already implementing `v4`
reuses its canonicalizer and signature verifier.

---

## 1. The three layers

```jsonc
{
  "schema": "cgr.cosign.v1",
  "profile": "cgr.disposition.v1",          // the payload profile; declares mode + required-ness (§5)
  "approval_mode": "bound" | "free",        // §5 — MUST equal the profile's declared mode

  "content_body": { /* … profile-defined payload … */ },   // Layer 1

  "approval_assertion": {                                   // Layer 2 (the approver's signed statement)
    "content_digest": "b2-256:…",           // BLAKE2b-256(JCS(content_body)) — §2.1
    "approver_id":     "…",                  // the natural-person principal (assurance layer, §9 / gap 3a)
    "approver_key_id": "ed25519:…",          // the public key the approver signature verifies against
    "approver_act":    "approve" | "modify" | "override",
    "decision_date":   "2026-09-06T…Z",      // when the human decided
    "record_nonce":    "…",                  // §4 — binds this approval to this one record
    "agent_draft_digest": "b2-256:…"         // §6 — REQUIRED for modify/override; absent for approve
  },
  "approver_signature": "ed25519-sig:…",     // Layer 3a — inner; over DOMAIN_TAG ‖ JCS(approval_assertion)

  "system_metadata": { "issuer": "…", "issuer_key_id": "ed25519:…", "recorded_at": "…" },
  "system_signature": "ed25519-sig:…"        // Layer 3b — outer; over the whole body minus envelope keys (§2.2)
}
```

- **Layer 1 — content body.** Profile-defined. Referenced everywhere by `content_digest` so the
  signatures bind to content without re-embedding it; large payloads MAY externalize sub-fields to
  content-addressed blobs (as `v4`/the ledger do), but `content_digest` is computed over the JCS of
  `content_body` as it appears in the record.
- **Layer 2 — approval assertion.** The approver's signed statement: *what* (`content_digest`), *who*
  (`approver_id`/`approver_key_id`), *what act* (`approver_act`), *when* (`decision_date`), *bound to
  which record* (`record_nonce`), and — for a modified/overridden decision — *what the agent had
  proposed* (`agent_draft_digest`).
- **Layer 3 — two nested signatures**: the inner **approver signature** (§3.1) and the outer **system
  signature** (§3.2).

---

## 2. Canonicalization

### 2.1 The content digest

`content_digest` MUST be `"b2-256:" ‖ hex(BLAKE2b-256(JCS(content_body)))`, lowercase hex, 64 hex
chars. `[FLAG]` BLAKE2b-256 (not SHA-256) is chosen to match grafomem's existing content-commitment
primitive (`src/aml/provenance.py`, and the gcrumbs Merkle node hash) so the envelope reuses one
content-hash across the stack; a reviewer preferring `v4`'s attestation-fingerprint algorithm can
override, at the cost of two content-hash algorithms in one ecosystem. A verifier MUST reject a record
whose `approval_assertion.content_digest` does not equal the recomputed digest of `content_body` (§8).

### 2.2 The approver's signed bytes

The approver signature is Ed25519 over exactly:

```
DOMAIN_TAG_BYTES ‖ JCS_BYTES(approval_assertion)
```

where `DOMAIN_TAG_BYTES` is the UTF-8 encoding of the domain tag defined in §9
(`grafomem.hitl.approval.v1:`), and `JCS_BYTES(approval_assertion)` is the RFC 8785 canonicalization of
the **entire** `approval_assertion` object (all fields present at signing time, including
`agent_draft_digest` when applicable). No prehash. The approver signature therefore covers **only** the
assertion — which is small, self-contained, and binds the content via `content_digest`. This is what
makes it **independently verifiable with only the approver's public key and the content body**, with no
dependency on the system signature (§3.1).

### 2.3 The system's signed body; envelope keys

The **envelope keys**, excluded from the system's signed body, are exactly: **`system_signature`** and
(if a profile uses one) **`evidence_ref`** — matching `v4`. The system signature is Ed25519 over:

```
JCS_BYTES( record  minus the envelope keys )
```

i.e. over `{schema, profile, approval_mode, content_body, approval_assertion, approver_signature,
system_metadata}`. **Crucially, `approver_signature` is inside the system's signed body.** No prehash.
Because the flat wire has no third location (as in `v4` §2.4), a present-but-unsigned `approver_signature`
is unrepresentable: it is either inside the signed body (covered) or a bare key the verifier
re-canonicalizes into the body anyway (making the system signature fail). This is the wire-level basis
of §7.

---

## 3. The two signatures

### 3.1 Approver signature (inner)

- MUST be produced by the **approver's own key** (`approver_key_id`), over the §2.2 bytes.
- MUST be **independently verifiable**: a verifier holding `content_body` + `approval_assertion` +
  `approver_key_id`'s public key MUST be able to verify it **without** the system signature. This is the
  accountability requirement — a supervisor confirms "this person signed this decision" without trusting
  the issuer.
- The approver signs the `content_digest` and their own `approver_act`, **not** the system signature
  (which does not yet exist at inner-signing time).

### 3.2 System signature (outer)

- MUST be produced by the issuer's key (`system_metadata.issuer_key_id`), over the §2.3 bytes, which
  **include `approver_signature`**.
- This is a **counter-signature**: the system attests "we captured *this* human-signed artifact."

### 3.3 Why nested, not parallel `[FLAG]`

Two detached signatures each over the content only (parallel) would let the approver block be **lifted
out** while the system signature — never having covered it — still verifies, yielding a valid
system-only record. **Nesting the system signature over a body that includes `approver_signature` is
what ties them**: removing or altering the approver signature changes the system's signed bytes and
breaks the outer signature (§7). This is decided, not optional; a reviewer who wants parallel signatures
must first solve the lift-out attack another way.

---

## 4. `record_nonce` and replay prevention

`record_nonce` binds an approval to **one record instance**. Without it, a valid approver signature over
identical content could be replayed to manufacture a second record the approver never intended (e.g.
re-submitting the same disposition under a different context).

- `record_nonce` MUST be unique per (issuer, approver_key_id): an issuer MUST NOT emit two records whose
  `approval_assertion` shares an `(approver_key_id, record_nonce)` pair, and a verifier/ledger that
  observes a duplicate MUST reject the later record.
- `record_nonce` MUST appear **inside `approval_assertion`** (so it is covered by the approver
  signature). `[FLAG]` It is deliberately not a separate top-level field to be cross-checked; putting it
  only in the signed assertion means a replay attempt cannot alter the nonce without breaking the
  approver signature.
- **Two replay vectors, two defenses.** *Different content:* prevented by `content_digest` — an approval
  cannot be moved onto content it did not sign. *Identical content, new record:* prevented by
  `record_nonce` uniqueness. Both are required; neither alone suffices.
- `[OPEN]` Whether `record_nonce` SHOULD additionally be a **challenge issued by the system before
  signing** (making the approval provably fresh, not just unique) or MAY be an approver-chosen random
  128-bit value. A system-issued challenge is stronger against a pre-signing replay but couples the
  approver's signing step to a system round-trip; left open for the profile/deployment.

---

## 5. Placements: the two modes, one construction

`cgr.cosign.v1` serves both modes from the 0009/CCR unification with **one construction**; the modes
differ only in **what `content_body` is** and **what it references**.

- **`approval_mode: "bound"`** — `content_body` **is** the decision being approved (e.g. an AML
  disposition). The approval gates *the action*. `approver_signature` MUST be present.
- **`approval_mode: "free"`** — `content_body` is a **transaction/approval record that references, by
  content-hash, the record(s) it approves** (e.g. a CCR learning transaction referencing experience
  records). The approval gates *a policy update*; the referenced experiences remain **separate,
  plain (single-system-signed) records**, kept raw. `approver_signature` MUST be present on the
  transaction; the referenced records MUST be identified by `b2-256:` hashes in `content_body`.

**How a payload declares its mode and required-ness.** The `profile` field names a profile registered
in a **profile registry** (a companion document; not this spec). Each registry entry declares:
`(profile_string, approval_mode, approver_signature: <required-ness>, referenced-records: none | required)`,
where `<required-ness>` is either **unconditional** (`REQUIRED`) or **conditional** (a predicate over
`content_body`, §5.1). A verifier MUST read the record's `profile`, look up its registry entry, and
enforce that entry's required-ness. `approval_mode` in the record MUST equal the profile's declared mode
(reject on mismatch). `[OPEN]` The registry's format and governance (closed-per-version like the `v4`
vocabularies, vs open) is left to the profile-registry document; the interim expectation is
**closed-per-version**, so consumers cannot disagree on a profile's mode or required-ness.

### 5.1 Conditional (predicate-based) required-ness

*(Added 2026-09-06 — see the amendment note in the header. The static form above could not express a
consumer that needs approval only for **some** records — doc 04's `HighRiskUpdate ⇒ RequiredApproval`.)*

A profile MAY declare `approver_signature` **REQUIRED when a predicate over `content_body` holds**,
instead of unconditionally. The predicate form is **deliberately narrow — a single (field-path,
operator, literal) triple, not an expression language:**

```jsonc
"approver_signature": {
  "required_when": { "field": "risk_class", "op": "eq", "value": "high" }
}
```

- **`field`** — a dot-path into `content_body` (e.g. `risk_class`, `delta.risk_class`). It MUST resolve
  to a scalar (string, number, or boolean); a path that is absent or resolves to a non-scalar makes the
  predicate **false** (the field cannot trigger a requirement it cannot evaluate) — but a verifier MUST
  surface `predicate_unresolved` so a misspelled path is not silently non-triggering.
- **`op`** — a **closed set**: `eq`, `ne`, `in`, `lt`, `lte`, `gt`, `gte`. `in` takes an array `value`.
  Ordering ops apply to numbers only (a non-number operand makes the predicate false + `predicate_unresolved`).
- **`value`** — a JSON scalar (or array for `in`). No references, no computation.

**Verifier obligation (normative).** A verifier MUST: (1) resolve the predicate against `content_body`;
(2) if it holds, treat `approver_signature` as REQUIRED and **reject** a record that satisfies the
predicate without a valid approver signature (the §7.2 non-conformance rule, now conditional); (3) if it
does not hold, the approver signature is OPTIONAL — but if one **is** present it MUST still verify
(§8.4). An **unconditional** `REQUIRED` is the degenerate predicate that is always true. A profile
declaring neither is malformed (reject at profile resolution).

**Why a predicate, not two profiles.** Routing a high-risk update to a "requires-approval" profile and a
low-risk one to a "no-approval" profile puts the **risk decision outside the signed record**: the
`risk_class` lives in `content_body` (signed), but *which profile was chosen* is a routing act with
nothing in the record to check it against. A **low-risk profile stamped on a high-risk update is then a
silent downgrade** — the verifier sees a self-consistent no-approval record and cannot know approval was
owed. The predicate keeps the requirement **verifiable from the record itself**: the verifier reads
`risk_class` from the signed body, evaluates the predicate, and *derives* that approval was required —
so a missing approver signature on a high-risk record is a rejection, not an invisible policy choice.
This is the same principle as putting the relation edge in the signed body rather than the envelope
(`v4` §2.4): a validity-affecting fact must be inside what the signature covers.

---

## 6. `agent_draft_digest` and `approver_act = modify | override`

The approver is accountable for the **final** content in `content_body`. When the human changed what the
agent proposed, the record MUST preserve what the agent had proposed:

- `approver_act = "approve"` — the final content **is** the agent's draft, unmodified.
  `agent_draft_digest` MUST be **absent**. `[FLAG]` absent rather than "equal to `content_digest`" — a
  redundant equal value is a second source of truth that can contradict; absence is unambiguous.
- `approver_act = "modify"` — the human edited the agent's draft. `agent_draft_digest` MUST be present
  and equal `BLAKE2b-256(JCS(original agent draft))`; `content_body` is the **final** (edited) content.
- `approver_act = "override"` — the human substituted a materially different decision (e.g. agent
  recommended *file*, human decided *no-file*). Same handling as `modify`: `agent_draft_digest` MUST be
  present, `content_body` is the human's final decision. The distinction from `modify` is **semantic
  and surfaced** (§8), not structural.

The agent's original draft body itself (not just its digest) SHOULD be retained by the issuer and
referenced/externalized so a supervisor can compare draft vs final; the envelope commits to the digest,
which is sufficient to prove *that* it differed and *what* it was, given the retained draft.

`[FLAG]` **Optional third signature — the agent signing its own draft — is DEFERRED from `v1`, not
adopted.** The record already attributes the draft to `content_body.agent_identity` (a profile field)
and preserves `agent_draft_digest`, so preparer *attribution* exists without a third signature. A
cryptographic agent signature would add preparer *non-repudiation*, but **no consumer in 0009 requires
it yet**, and adding a third signature now fixes its canonicalization and verifier obligations before
anything exercises them — the same reasoning by which `v4` declined to ship `corrects` speculatively. A
reviewer who has a concrete consumer (e.g. a dispute where the agent's authorship is contested) can
overturn this and add `agent_signature` as a third nested layer under a `cgr.cosign.v2` or an additive
`[OPEN]`-resolved extension. Recorded as flagged, not closed.

---

## 7. Strippability — the tamper-evidence register

Stated in the same register as the Experience-Ledger honesty clause and [0006](../decisions/0006-enforcement-boundary-for-revocation.md):
**tamper-evident and conformance-enforced, not tamper-proof.**

1. **Stripping the approver signature produces an INVALID record.** Because the system signature covers
   a body that includes `approver_signature` (§2.3), removing or altering it breaks the outer signature.
   Cryptographically enforced; requires no envelope-specific logic beyond signature verification.
2. **An approver-less record for which approval is required is NON-CONFORMANT.** A verifier MUST
   reject a record whose `profile` requires an approver signature — **unconditionally, or via a §5.1
   predicate that the record's `content_body` satisfies** — and which lacks a valid approver signature
   (§5, §5.1, §8.3a-4). Because the predicate is evaluated over the *signed* `content_body`, a high-risk
   record cannot dodge the requirement by omitting the signature: the verifier derives that approval was
   owed. This is a **verifier rule**, not a property of the bytes — the enforce-or-label posture of
   [0006]; a consumer that does not enforce it is the hole. `[0006-B]` *which* consumers are obligated to
   enforce (all, vs only chain-writers) inherits 0006 Question B.
3. **A compromised or re-minting issuer can always sign a fresh approver-less record.** No envelope
   stops the holder of the system key from signing a different artifact. This is **detectable against
   externally-held commitments** — the record anchored into the append-only gcrumbs chain with
   externally-anchored epochs — but **not preventable**. Detection, not prevention (the ledger §5.3
   "checkpoints must leave the system" rule applies unchanged).

The honest one-line claim a profile MAY state: *"the approval is non-strippable for conformant records
against a non-issuer attacker; against a compromised issuer it is detectable, not preventable."*

---

## 8. Verifier obligations

A conformant `cgr.cosign.v1` verifier MUST perform these checks **in this order**, failing closed on
the first failure:

1. **Schema.** `schema == "cgr.cosign.v1"`; else out of scope (reject in an enforcing context).
2. **Profile resolution.** Resolve `profile` in the registry; obtain its `approval_mode` and
   required-ness (unconditional, or a §5.1 predicate). MUST reject if `profile` is unknown (fail closed)
   or if `record.approval_mode` ≠ the profile's declared mode.
3. **Content integrity.** Recompute `BLAKE2b-256(JCS(content_body))`; MUST equal
   `approval_assertion.content_digest`. Reject on mismatch. *(Do this before step 3a — the predicate is
   evaluated over the content that step 3 just proved matches the digest.)*
   - **3a. Required-ness resolution (§5.1).** Evaluate the profile's required-ness against `content_body`:
     unconditional ⇒ REQUIRED; predicate ⇒ REQUIRED iff it holds. Surface `predicate_unresolved` if a
     predicate field is absent/non-scalar (treated as not-holding). The result — "approver signature
     required: yes/no" — drives step 4.
4. **Approver signature.** If required (per 3a) and **absent**, MUST reject (§7.2 non-conformance,
   conditional per §5.1). If **present** (whether required or not), recompute
   `DOMAIN_TAG_BYTES ‖ JCS(approval_assertion)` and verify the Ed25519 `approver_signature` against
   `approver_key_id`; MUST reject if invalid. (An optional-and-absent approver signature is conformant.)
5. **Approval binds this content.** Confirm `approval_assertion.content_digest` equals the digest
   computed in step 3 (i.e. the signature verified in step 4 is over *this* content). (Redundant with 3
   only if the assertion was not tampered; kept explicit because it is the property that matters.)
6. **System signature.** Recompute `JCS(record minus envelope keys)`; verify the Ed25519
   `system_signature` against `system_metadata.issuer_key_id`. MUST reject on failure — **this is the
   check that catches a stripped or altered approver signature (§7.1).**
7. **Nonce.** `record_nonce` is inside the signed assertion (so already covered); the verifier/ledger
   MUST enforce `(approver_key_id, record_nonce)` uniqueness (§4) and reject a duplicate.
8. **Act/draft consistency.** For `approver_act ∈ {modify, override}`, `agent_draft_digest` MUST be
   present; for `approve`, absent (§6). Reject on violation.
9. **Free mode references.** For `approval_mode == "free"`, the referenced record hashes MUST be
   well-formed. `[OPEN]` whether the verifier MUST *resolve* (fetch and verify existence of) the
   referenced experiences, or MAY surface them as unresolved — proposed default: **degrade like the
   `v4` `continues` edge** (surface `references_unresolved` without rejecting), since the approval's
   validity is about the transaction, not the retrievability of what it points at.

**What the verifier MUST surface (not gate on):** `approver_id`, `approver_act` (including whether the
decision was `modify`/`override` vs `approve`), `decision_date`, and the **assurance tier** of
`approver_key_id` (how strongly the key is bound to a named person — §9). Per the `v4` precedent for
`evidence_tier`/`verifiability_tag`, the verifier **surfaces** the assurance tier and **MUST NOT gate**
on it: whether an `operator_verification`-grade approver identity is *sufficient* is the **relying
party's** judgment, not the verifier's. `[0006-B]` whether a consumer is *obligated* to seek the
approver's assurance before relying is the same open enforcement-boundary question.

---

## 9. The domain tag and composition with `gns_browser`

`DOMAIN_TAG` is the exact ASCII string **`grafomem.hitl.approval.v1:`** (including the trailing colon),
UTF-8. It is **adopted from `gns_browser`'s existing signer**, whose `signBytesToHex` refuses to sign
any payload lacking that prefix (`identity_wallet.dart:179-202`). Adopting it is deliberate: domain
separation prevents the approver key's signatures being replayed across protocols, and it means the
already-deployed self-custody signer **fits as-is**.

- The self-custodied Ed25519 signer and its key custody are **unchanged**.
- The **only** change is caller-side: the caller MUST construct the structured `approval_assertion`,
  JCS-canonicalize it, prepend `DOMAIN_TAG`, and hand *those* bytes to the signer — instead of an opaque
  payload. `approver_act` MUST be inside those bytes.
- The domain tag version (`…approval.v1`) is bound to this envelope version; a `cgr.cosign.v2` would use
  `…approval.v2:` so signatures cannot cross versions.

---

## 10. What this spec deliberately does not do

- **Does not resolve the approver-identity assurance (0009 gap 3a).** The envelope verifies that
  `approver_key_id` signed; **binding that key to a named, accountable, verified person** (bank-vouched
  enrolment, or a QES co-signature) is the **separate assurance layer** and is out of scope. The
  envelope surfaces the assurance tier (§8) but does not define how it is established. `approver_id` and
  `approver_key_id` resolution is that layer's responsibility.
- **Does not define the profile registry**, nor any specific profile (`cgr.disposition.v1` is authored
  separately, product-side).
- **No corpus, no fixtures, no implementations.** Conformance vectors (a stripped-approver vector, a
  lifted-to-different-content vector, a replayed-nonce vector, a modify-without-draft-digest vector, a
  valid two-signature vector) and the two verifier implementations are the next step, in the same order
  `v4` followed (spec → corpus → implementations).
- **Does not decide `[0006-B]`** (who is obligated to enforce/seek), the `record_nonce` challenge
  question (§4), the free-mode resolution obligation (§8.9), the content-digest algorithm `[FLAG]` (§2.1),
  or the third-signature `[FLAG]` (§6).

---

## 11. Open questions (consolidated)

1. `[FLAG]` content-digest algorithm: BLAKE2b-256 (chosen, matches grafomem primitives) vs `v4`'s
   attestation-fingerprint algorithm (§2.1).
2. `[OPEN]` `record_nonce`: system-issued challenge (freshness) vs approver-chosen random (§4).
3. `[OPEN]` profile registry format/governance — closed-per-version vs open (§5).
4. `[FLAG]` `agent_draft_digest` for `approve`: absent (chosen) vs equal-to-`content_digest` (§6).
5. `[FLAG]` third signature (agent signs its own draft): **deferred from v1** — overturn with a concrete
   consumer (§6).
6. `[OPEN]` free-mode reference resolution: MUST resolve vs surface-unresolved (proposed: degrade) (§8.9).
7. `[OPEN]`/`[0006-B]` enforcement boundary: which consumers MUST enforce approver-signature presence
   (§7.2) and MUST seek the approver's assurance tier before relying (§8) — inherits 0006 Question B.
8. `[OPEN]` **gap 3a** — the approver-identity assurance layer — is out of scope here and required
   before a profile's approver identity is a *verified named person* (§10).
