# CGR Attestation `v4` — specification (PROPOSED)

- **Status:** **Proposed** — P1.1 design document. Not implementation; no code, no fixtures regenerated.
- **Schema string:** `cgr.attestation.v4`
- **Date:** 2026-09-01
- **Inputs:** decision records [0001](../decisions/0001-cgr-grounding-dimension-additive-vs-schema-bump.md),
  [0002](../decisions/0002-cgr-governance-domain-and-backfill.md),
  [0004](../decisions/0004-no-identity-continuity-across-rotation.md),
  [0005](../decisions/0005-custody-managed-principals.md),
  [0006](../decisions/0006-enforcement-boundary-for-revocation.md).
- **Audience:** the three consumers that implement against this — `@gns-foundation/cgr-verify`
  (reference verifier), `@geiant/core`, and the CGR read surface — plus the issuer.

This document uses **MUST / SHOULD / MAY** per RFC 2119. Three marker conventions appear throughout:

- **`[OPEN]`** — a question this spec deliberately does **not** resolve; flagged, not hidden.
- **`[0006-B]`** — wording whose meaning depends on decision [0006](../decisions/0006-enforcement-boundary-for-revocation.md)
  Question B (uniform vs scoped enforcement). Consolidated in §4. **Do not read these as decided.**
- **`[FLAG]`** — a place where this spec made a call that could reasonably go the other way; the
  reasoning is stated so a reviewer can overturn it deliberately.

---

## 0. Framing — what `v4` is

Read across the records, the thing that unifies 0001/0002/0004 is **not** the relation edge — each
needs something different.
[0001](../decisions/0001-cgr-grounding-dimension-additive-vs-schema-bump.md) needs new signed
**fields** (an oracle identity, an audit-policy digest);
[0002](../decisions/0002-cgr-governance-domain-and-backfill.md) needs a **domain value** and a
**`decision_date`**; only [0004](../decisions/0004-no-identity-continuity-across-rotation.md) needs a
**relation edge**. So a relation edge cannot by itself close 0001 or 0002 — those are new fields and
values, not relations. What the three *do* share is the prior question every signed addition faces:
**signed-body vs envelope** (0002 asks it of `decision_date`, 0004 Q1 of the edge, and both say it
should be answered the same way), **fixed enum vs open vocabulary** (0002's domain, 0004 Q2's relation
types — the same axis), and **additive vs schema-bump** (0001's whole axis, inherited by 0002 and
0004). That shared question — **how CGR adds signed meaning** — is the unifier. P0.4 resolved it to a
**schema bump**. `v4` is the first exercise of that policy, and it carries three independent additions
that happen to ship together:

1. a typed, signed **relation edge** (`relates_to`) — closes 0004, enables 0005-grandfathering,
   carries revocation (§1, §3);
2. **grounding** signed fields (0001);
3. a **governance domain** + **temporal provenance** (0002).

Only (1) is a relation between attestations. (2) and (3) are new scalar/vocabulary fields that ride
the same bump. Keeping this distinction visible is the point: we are not "adopting an edge and hoping
it closes grounding."

**Carried forward from `v3`, unchanged:**

- Canonicalization: **RFC 8785 (JCS)**. Signature: **Ed25519 over the JCS-canonical bytes of the
  signed body, with no SHA-512 prehash.**
- **Envelope keys** (excluded from the signed body and the signature): `signature`, `evidence_ref`.
  `v4` adds no new envelope keys. `[FLAG]` every new field defined below is in the **signed body** —
  see §2.4 for why an envelope-carried validity-affecting field would be incoherent.

---

## 1. The relation edge

### 1.1 Field shape

```jsonc
"relates_to": [
  { "type": "continues" | "supersedes" | "revokes",
    "target": { "kind": "attestation" | "delegation_cert",
                "hash_alg": "sha-256",
                "hash": "<hex>" } }
]
```

- `relates_to` is an **array** of edges. Absent or `[]` ⇒ no relations. `[FLAG]` a list, not a single
  edge: an attestation can carry more than one relation (e.g. a re-issuance that both `continues` a
  rotated identity and `supersedes` a prior cert). Most attestations carry 0 or 1; the array costs
  nothing and avoids a second schema change the first time two are needed. If reviewers prefer a
  single optional object, the traversal rules in §1.3 are unchanged.
- `target` is an **object**, not a bare hash, because targets are cross-type (an attestation vs a
  delegation certificate) and the verifier must know which without guessing. `kind` and `hash_alg`
  are **REQUIRED**.
- `target.hash`:
  - for `kind: "attestation"` — the **SHA-256 of the target attestation's JCS-canonical signed body**
    (the same content-address the read surface already uses to fingerprint an attestation).
  - for `kind: "delegation_cert"` — the delegation certificate's `cert_hash`.
    **`[FLAG]`/`[OPEN]`** this is the exact hazard raised in `GNS-Foundation/geiant#10`: the geiant
    reference implementation has **two** "cert hash" functions that disagree (`setup-agent.ts` prints
    a truncated-SHA-512; the runtime computes SHA-256). This spec pins `target.hash` for a
    delegation-cert target to **the runtime SHA-256 of the certificate's JCS-canonical signed body,
    principal_signature excluded** — the value the enforcing engine actually stores and looks up. Any
    consumer computing this differently will fail to resolve targets. This must be resolved (fix #10)
    before the first cert-targeting edge is issued, or the addressing is ambiguous across
    implementations.

**Multiplicity — repeated edges of the same type.** Because `relates_to` is an array, an implementer
will immediately hit "what do two edges of the same type mean?" The answer is normative, not a
follow-up:

- **Duplicate edge — identical `{type, target}` pair: MUST reject** the attestation as malformed. A
  repeated edge carries no additional meaning and is a signal of a construction bug or tampering; it
  is never valid.
- **`continues` — at most one, period. Two `continues` edges (even to distinct targets) MUST be
  rejected.** An identity has **at most one lineage predecessor**; two would assert a *merge* of two
  identities into one, which is not a rotation, has no consumer in 0001–0006, and breaks the
  linear-lineage guarantee. This is the **subject-side dual** of the ceremony's anti-fork rule
  (§5.3.4, at most one successor per predecessor): the two together make the continuity graph a set of
  strictly linear chains — no forks, no merges.
- **`supersedes` — multiple permitted, to distinct targets.** One re-issuance may legitimately
  supersede several prior attestations (consolidation). Targets MUST be distinct (duplicates fall
  under the reject rule above).
- **`revokes` — multiple permitted, to distinct targets.** One signed action may revoke a batch of
  distinct targets. Targets MUST be distinct.

So: `continues` is singular; `supersedes`/`revokes` may repeat across distinct targets; any exact
`{type, target}` duplicate is malformed. `[FLAG]` the batch-`revokes`/`supersedes` allowance is a
call — a reviewer who wants "one target per attestation, always" can tighten it, at the cost of
forcing N attestations to revoke N certs. It is allowed here because a batch revocation is a single
authorized decision and splitting it loses that atomicity in the record.

### 1.2 Vocabulary — justify each member or drop it

Per the instruction that "a vocabulary that ships with an unused verb is a vocabulary that will grow
badly," each candidate is held to the test: **is there a concrete consumer in 0001–0006?**

| verb | concrete consumer | validity-affecting? | ship? |
|---|---|---|---|
| `continues` | 0004 rotation continuity; 0005 identity model | no (additive history) | **yes** |
| `supersedes` | 0005 grandfathering of ephemeral-principal certs; re-issuance | yes | **yes** |
| `revokes` | 0004 revocation event; geiant#9 (§3) | yes | **yes** |
| `corrects` | **none in 0001–0006** | (would be) yes | **DROP** |

**`corrects` is dropped from `v4`.** `[FLAG]` It has no consumer in any current record. The
distinction it would carry — a target that was *wrong* (never valid) versus one that is merely
*stale* (`supersedes`, valid in its window) — is real and a verifier would have to treat them
differently (a corrected attestation must be scrubbed from history, a superseded one retained as
once-valid). But shipping that distinction now, with nobody exercising it, guarantees consumers
implement it inconsistently before the first real correction ever occurs. The versioning policy (§0)
exists precisely so a verb can be added when a consumer appears; adding `corrects` "just in case"
spends that policy for nothing. If a correction case arises, it lands as a defined addition, with the
never-valid semantics specified against a real example.

The `v4` vocabulary is therefore **closed at `{continues, supersedes, revokes}`**. See §1.3 on why
"closed" is load-bearing.

### 1.3 Traversal — what a verifier MUST do

**Governing principle — Lineage-Degrades, Validity-Fails-Closed.** When a traversal cannot complete
(a cycle, or the depth bound below), behaviour is decided by the edge class, not the cause:

- **Lineage-only (`continues`):** the edge does not affect the subject's validity, so an incomplete
  traversal MUST **degrade** — accept the subject, mark the lineage incomplete, and report *why* via
  `lineage.status` (below). Never reject the subject for a lineage problem.
- **Validity-affecting (`supersedes`, `revokes`):** the traversal answers a validity question ("is
  this current / revoked?"). An incomplete traversal leaves that question **undeterminable**, and an
  undeterminable validity answer MUST resolve to **reject** — never to "valid."

This one rule resolves both the cycle and depth-bound cases below; they are the same question seen
twice. The `unrecognized type` rule is its degenerate case (an unprocessable validity-affecting edge
⇒ reject).

**`lineage.status` — the verifier's lineage signal (distinct, machine-readable states).** When a
verifier reconstructs `continues` lineage it MUST report one of these, **not** a single boolean plus
prose:

| `lineage.status` | meaning | anomaly? |
|---|---|---|
| `complete` | full lineage reconstructed to the root | no |
| `truncated_unavailable` | a predecessor could not be obtained (pruned / offline ledger) | **no** — expected, benign |
| `truncated_depth` | the depth bound was reached first | **no** — benign |
| `anomaly_cycle` | a cycle was detected in the `continues` graph | **YES** — corrupt Foundation-signed data or tampering; alertable |

`truncated_unavailable` and `anomaly_cycle` both leave lineage incomplete but mean **completely
different things** — a missing ledger entry vs. a Foundation-signed contradiction. A consumer MUST be
able to alert on `anomaly_cycle` **without** alerting on `truncated_unavailable`. Emitting one flag
with different prose for the two is **non-conformant**.

**General rules (all types):**

- **Unrecognized `type`: MUST reject** (fail closed). A `v4` consumer is expected to understand the
  entire `v4` vocabulary; an unknown verb in a `v4` body is either corruption or an out-of-spec
  issuer, and a validity-affecting edge that is silently ignored is exactly the Finding-4 failure
  (an old verifier trusting stale/revoked data). `[FLAG]` This makes the vocabulary **closed per
  schema string**: a new verb cannot be added additively under `v4` — it requires a new versioning
  event (a `v5`, or a `crit`-style extension convention if one is later adopted). This is the direct
  reason `corrects` should not ship speculatively (§1.2): you cannot cheaply walk it back.
- **Cycle detection: MUST**, applying the governing principle. A verifier following a chain MUST
  track visited `target.hash` values and MUST treat a revisit as a traversal failure rather than
  looping. Behaviour by the traversal's type:
  - **`continues` cycle → degrade + flag.** The `continues` graph is provably a set of linear chains
    given both uniqueness rules (§1.1 at most one `continues` per attestation; §5.3.4 at most one
    successor per predecessor), so a cycle cannot arise from honest issuance — it means
    Foundation-signed contradiction, tampering, or (negligibly) hash collision. But `continues` is
    not validity-affecting, so: subject `valid: true`, `lineage.status = anomaly_cycle`,
    `lineage.truncated_at = <revisited hash>`. Do **not** reject the subject; **do** raise the
    anomaly (distinct from `truncated_unavailable`).
  - **`supersedes` cycle → reject.** A cycle makes the subject simultaneously current (chain head)
    and stale (in the loop); currency is undeterminable ⇒ subject `valid: false`, reason
    "supersedes chain contains a cycle."
  - **`revokes` cycle → reject.** Incoherent revocation state ⇒ subject `valid: false`, reason
    "revokes chain contains a cycle." Revocation data must never resolve to "not revoked" by default.
  - **Mixed-type cycle.** Traversal is normally per-type/per-purpose (reconstruct lineage via
    `continues`; find the current head via `supersedes`; check revocation via `revokes`), so a cycle
    is within one type. If an implementation does cross-type traversal and the cycle contains **any**
    `supersedes` or `revokes` edge, it MUST **reject** — the most-conservative rule wins.
- **Depth bound: MUST.** A verifier MUST bound traversal depth and MUST support **at least 64** hops
  before it MAY stop. On reaching the bound it applies the governing principle: a `continues`
  traversal reports `lineage.status = truncated_depth` (subject `valid: true`); a
  `supersedes`/`revokes` traversal **rejects** (the validity question is unresolved within the bound).

  **Why 64** — stated so a future reviewer can revise it without re-deriving the argument. The
  minimum must **exceed the longest legitimate chain** while **bounding adversarial depth**:
  - *Rotations (`continues`):* rotation is rare (key compromise / policy); the first GEIANT rotation
    reached lineage length 1. Even monthly rotation for five years is 60; realistic identities see
    single digits over a lifetime. 64 clears the pathological case with margin.
  - *Consolidation (`supersedes`):* the primary use is one-time 0005 grandfathering (length 1) and
    occasional corrections — not per-score re-issuance (a fresh attestation with a newer `as_of` is
    naturally more current without an explicit edge). A `supersedes` chain approaching 64 signals that
    re-issuance should point at a stable root instead of chaining; the bound is a forcing function
    against that anti-pattern, not a limit on legitimate use.
  - *Adversarial ceiling:* 64 hash-fetch-and-verify hops is cheap for an honest verifier yet caps the
    work a maliciously deep chain can force.

  A conformant verifier MAY support more than 64; 64 is the floor all consumers can rely on, and the
  conformance suite pins it so consumers agree.
- **Target unreachable** is defined per type below. "Unreachable" = the verifier cannot obtain the
  target body/cert (offline, not in the ledger it can see, or pruned).

**Per type:**

- **`continues(target)`** — asserts *the subject identity is the continuation of the target
  identity*. A verifier reconstructing history across a rotation:
  - MUST verify the edge is **Foundation-issuer-signed** (§5) before trusting it; an agent-signed
    `continues` MUST NOT be trusted (the outgoing key may be the compromised one — 0003/0004).
  - `continues` is **not validity-affecting for the subject**: the subject attestation is valid on
    its own; the edge only adds predecessor lineage. Therefore, on **unreachable target**, the
    verifier MUST **degrade gracefully** — subject `valid: true`, `lineage.status =
    truncated_unavailable` (a benign, expected state — pruned/offline ledger — and **distinct** from
    `anomaly_cycle`; see the signal table) — and MUST NOT reject the subject. Rejecting a live agent
    because its (possibly-revoked, possibly-pruned) predecessor is unreachable would reintroduce the
    punitive-rotation problem 0004 exists to remove.
- **`supersedes(target)`** — asserts *the target is no longer current*. Validity-affecting.
  - A verifier that **holds the superseding attestation** and is evaluating the **target** MUST treat
    the target as stale (not current).
  - **Asymmetry (§3 applies here too):** the edge lives on the *new* attestation, not the old one. A
    verifier holding only the target cannot know it was superseded — that is a **liveness query**
    ("are there `supersedes` edges pointing at this?"), not a property of the target. Who is obligated
    to run that query is `[0006-B]`.
  - **Unreachable target:** the superseding attestation is valid on its own; the verifier MUST record
    "supersedes `<hash>` (target unavailable)" and MUST NOT reject the superseding attestation. It
    cannot confirm the target existed, but the claim is signed.
- **`revokes(target)`** — asserts *the target is revoked as of this record*. Strongest validity
  impact. Full treatment in §3. In brief: a verifier holding a `revokes` edge and evaluating the
  target MUST refuse the target; discovering revocation from the target alone is a liveness query
  `[0006-B]`; unreachable target ⇒ the revocation claim still stands (signed).

---

## 2. The `v4` delta over `v3`

### 2.1 `v3` signed body (carried forward unchanged)

From the `v3` golden wire lock, the signed body is these 18 fields (JCS-sorted):
`agent_handle`, `as_of`, `capability_tier`, `cgr_score`, `confidence`, `dimension`,
`domain_n_resolved`, `issuer`, `issuer_key_id`, `last_resolved_at`, `n_resolved`, `rationale`,
`requested_domain`, `schema`, `scoring_scope`, `subject_did`, `subject_key`, `tier`. All are retained
in `v4` with unchanged meaning, except `schema` (§2.2).

### 2.2 New and changed fields

| field | req/opt | notes |
|---|---|---|
| `schema` | **REQUIRED** | value becomes `"cgr.attestation.v4"`. This is the whole safety mechanism (§2.3). |
| `relates_to` | **OPTIONAL** | array of edges (§1). Absent/`[]` = no relations. |
| `oracle_id` | **REQUIRED for grounding-class `dimension`; MUST be absent otherwise** | 0001. Identity of the resolution oracle. |
| `audit_policy` | **REQUIRED for grounding-class `dimension`; MUST be absent otherwise** | 0001. Digest of the pre-registered audit policy. |
| `n_unresolvable` | **OPTIONAL** (grounding-class only) | 0001. Uncertainty-mass count; absent ⇒ 0. |
| `domain` | **REQUIRED** | 0002 gap (a). The subject/capability domain, vocabulary **extended** to include governance/strategy/compliance. Fixed-enum-vs-open is `[OPEN]` (§2.5). |
| `verifiability_tag` | **REQUIRED** | 0002. `judgment` (moves a score) vs `rule` (recorded, non-scoring). Governance records use `rule` so they sit on the chain without polluting reputation. `[FLAG]` promoted into the signed body (it was a capture-path field) because whether a record scored an agent is tamper-evident-relevant, not advisory. |
| `decision_date` | **REQUIRED** | 0002 gap (b). When the decision was made. In the **signed body** (§2.4). |
| `recorded_at` | **REQUIRED** | 0002. When this attestation was captured/issued. `decision_date == recorded_at` ⇒ contemporaneous. |
| `backfilled` | **REQUIRED** | 0002. Boolean; `true` ⇒ recorded after the fact. Redundant with `decision_date < recorded_at` by design — an explicit flag is harder to misread than a timestamp comparison. |

**Grounding-class detection — resolved: infer from `dimension`.** Grounding-class is determined
**solely** by the `dimension` value:

```
is_grounding  ≡  dimension ∈ GROUNDING_DIMENSIONS      # a CLOSED, normative set (see below)
```

No separate boolean. A boolean would be a second source of truth that can contradict `dimension`,
and catching that contradiction requires validating the boolean against this same set anyway — so a
boolean is strictly more surface for the same guarantee. Inference also matches
[0001](../decisions/0001-cgr-grounding-dimension-additive-vs-schema-bump.md), which models grounding
as a `dimension` value. The coupling this creates — the `dimension` value gates required-field
validation — is made safe by **closing the set**: `GROUNDING_DIMENSIONS` is normative and part of the
`v4` schema string, extendable only by a versioning event, exactly like the relation vocabulary
(§1.3). A fuzzy/open set would let the three consumers disagree on class membership and therefore on
whether `oracle_id`/`audit_policy` are required — a security-relevant inconsistency; closing it
removes that.

**Required-field gate** (keys off `dimension` only; independent of the `domain` open-vs-closed
question, §2.5):

```
is_grounding      → oracle_id, audit_policy REQUIRED (reject if absent); n_unresolvable OPTIONAL
not is_grounding  → oracle_id, audit_policy, n_unresolvable MUST be absent (reject if present)
```

**`GROUNDING_DIMENSIONS` (closed, normative).**

```
GROUNDING_DIMENSIONS = { "grounding" }
```

A single member: an attestation is grounding-class **iff `dimension == "grounding"`**. The grounding
model specifies a grounding attestation as a standard mint carrying `dimension: "grounding"` —
grounding is *a second value of the existing `dimension` field*, not a parallel field set. The value
is pinned **here**, normatively, so this spec stands alone (a consumer needs nothing beyond this
document to implement the gate). Adding a future grounding-class dimension is a versioning event —
the set is closed per schema string.

**Grounding is orthogonal to the capability domain, not exclusive with it.** `dimension` says *what
kind of outcome resolves the judgment* (a receivables outcome vs. a grounding audit); the capability
**domain** (the `domain` row above; `cgr_domain` in the grounding model) says *what area the judgment
concerns*. They are independent axes. So grounding is mutually exclusive only with **other outcome
kinds** — an attestation resolves exactly one kind of outcome — while a grounding judgment **within
any domain is fully expressible**: e.g. `dimension: "grounding"`, `domain: "deploy"` is a grounding
audit of a deploy claim. There is no domain-orthogonality limitation and no trade to reverse.

`[OPEN]` **temporal-field overlap.** `v3` already carries `as_of` and `last_resolved_at`. `v4` adds
`decision_date` and `recorded_at`. These are distinct in intent — `as_of`/`last_resolved_at` describe
*score/data currency*; `decision_date`/`recorded_at` describe *when the decision was made vs
attested* — but a consumer could conflate `as_of` with `decision_date`. The spec must state the
relationship explicitly before issuance (candidate: `as_of` ≥ `last_resolved_at`; `decision_date` is
independent of both; `recorded_at` ≥ `decision_date`). Flagged so it is decided, not discovered.

### 2.3 What a `v3` consumer sees

A `v3` consumer gates acceptance on `schema ∈ ACCEPTED_SCHEMAS`, and `"cgr.attestation.v4"` is not in
that set. **It therefore rejects a `v4` attestation at the schema check, without parsing the body.**
This is the intended, safe behavior, and it is why this is a **bump, not additive**. The reasoning is
re-derivable from the public verifier source. Per
[0001](../decisions/0001-cgr-grounding-dimension-additive-vs-schema-bump.md), the deployed verifiers
(e.g. `@gns-foundation/cgr-verify`) verify the signature over the whole non-envelope body and gate
acceptance **only** on the schema string — so a *new signed field* verifies additively under `v3`,
while a *new schema string* is rejected until `ACCEPTED_SCHEMAS` widens. It follows directly that under
pure-additive `v3` a verifier which checks signature + schema but never reads `relates_to` would
**accept and ignore** a `supersedes`/`revokes` edge — silently treating superseded or revoked data as
current. For a validity-affecting field that is the dangerous case. A schema bump instead makes such a
verifier **fail closed** — it rejects `v4` at the schema check — rather than fail silent.

Consequence for rollout (expand-contract): because a `v3` consumer rejects `v4` at the schema check,
emitting `v4` before consumers accept it would make every un-updated consumer reject legitimate
attestations. So every consumer MUST widen `ACCEPTED_SCHEMAS` to include `v4` **and** implement §1.3
traversal **before** the issuer emits any `v4` attestation. Reference verifier leads; `@geiant/core`
and the read surface follow; issuance is last.

### 2.4 Why every new field is in the signed body

0002 (for `decision_date`) and 0004 Q1 (for the edge) both ask "signed body or envelope?" and both
say the answer should be the same. **This spec answers: signed body, for all of them.** `[FLAG]` A
validity-affecting relation carried in the *envelope* would be **unsigned and therefore forgeable** —
an attacker could strip a `revokes` edge or forge a `continues`. An edge whose entire purpose is to
change how a relying party treats an identity cannot live outside the signature. The same logic makes
`decision_date`/`backfilled` signed (temporal provenance a supervisor may examine must be
tamper-evident, per 0002's "first-class" requirement). This resolves the shared open question
consistently rather than per-field.

### 2.5 `[OPEN]` — domain and relation vocabulary: fixed enum vs open

0002 asks it of `domain`; 0004 Q2 asks it of relation types. Same axis, and this spec leaves it
**open** for the same reason 0006 leaves Question B open — it is a standard-governance decision, not
a wire-format one. Interim position: the **relation vocabulary is closed** (§1.3, unknown = reject,
because it is validity-affecting), while the **`domain` vocabulary may be open** (an unknown domain on
a `rule`/non-scoring record is not validity-affecting the way an unknown edge is). If reviewers want
both closed, §1.3's reject rule extends to `domain`; note that would make every new domain a
versioning event.

---

## 3. Revocation

Revocation is expressed as a **`revokes` edge** (§1) **plus** an **enforcement index** — not one or
the other. [0004](../decisions/0004-no-identity-continuity-across-rotation.md)'s decision already draws
this two-surface distinction (the edge is the signed record; discovering *current* revocation stays a
liveness query against `geiant#9`'s `agent_registry.revoked_at`), and it follows from first principles
about append-only chains, as the bullets below derive.

- **The edge is the record.** A `revokes` edge is the signed, attributable, offline-auditable record
  *that a revocation happened* and who asserted it. It is retained forever (append-only).
- **The edge cannot answer "is X *currently* revoked?"** This asymmetry is **normative and stated
  here so it is not discovered later.** Revocation is a *negative, forward-in-time* claim: an offline
  verifier holding only the original (target) attestation has no way to know a later `revokes` edge
  exists. Determining current status is a **liveness query against the ledger** — "are there
  `revokes` edges targeting X, and is the most recent one still in force?" — which is a property of
  *the ledger at query time*, not of the attestation.
- **The enforcement index is the query surface.** geiant#9's `agent_registry.revoked_at` /
  `delegation_certificates.revoked_at` is the O(1) enforcement index. The schema expresses the
  *event*; the index answers the *liveness question*. A `v4` implementation MUST NOT pretend an
  append-only chain gives revocation-checking for free (that would turn an O(1) check into a chain
  scan).
- **Normative statements:**
  - An issuer revoking a target MUST emit a `revokes` edge (the record) **and** update the
    enforcement index (the liveness surface). Emitting only one is a defect: an edge without an index
    entry is unenforceable in practice; an index entry without an edge is unauditable offline.
  - A verifier holding a `revokes` edge and evaluating its target MUST refuse the target.
  - **Whether a verifier that does *not* hold the edge is obligated to perform the liveness query
    before trusting an attestation is `[0006-B]`** — see §4. This is the single most important
    consequence of leaving 0006 open: today, "revoked" binds only where someone runs the query.

`[OPEN]` **revocation of a revocation** (un-revoke) and **future-dated revocation** (`revoked_at` in
the future, honored only once reached — geiant#9 already implements the latter for the column). The
edge form of these is unspecified here; do not assume symmetry with the column until specified.

---

## 4. Where 0006 bites — the concrete list

[0006](../decisions/0006-enforcement-boundary-for-revocation.md) Question B (uniform vs scoped
enforcement) decides **who is obligated to honour a revocation/supersession**. This spec does **not**
resolve it. Every location whose wording depends on the answer is tagged `[0006-B]` above and
enumerated here, so 0006 can be settled against a concrete list rather than in the abstract:

1. **§1.3 `revokes` traversal** — "a verifier … MUST refuse the target" is unambiguous *when the
   verifier holds the edge*. Whether **every** identity-holding consumer must **seek** the edge
   (run the liveness query) before trusting an attestation is B. Under **B1 (uniform)** every consumer
   must; under **B2 (scoped)** only chain-writers must, and read-only display consumers are exempt.
2. **§1.3 `supersedes` liveness** — same shape: must a consumer check for superseding edges before
   presenting a target as current, or only honour one it already holds? B.
3. **§3 enforcement-index liveness obligation** — "whether a verifier that does not hold the edge is
   obligated to perform the liveness query" is B verbatim.
4. **§3 dual-write requirement** — "an issuer … MUST emit an edge **and** update the index" assumes
   the index is the universal enforcement surface. Under B2, non-enforcing consumers never read the
   index, so the requirement binds issuers but guarantees nothing about consumers — B decides whether
   that gap is acceptable.
5. **Conformance (0006 Open Q2, standard jurisdiction)** — whether "a conformant `v4` verifier"
   **requires** revocation-liveness enforcement, or whether enforcement is an integrator
   responsibility the spec only documents. This is the one B-dependent item that is a *standard*
   decision, not a geiant architecture choice, and it changes what the conformance suite asserts.

Not B-dependent (stated to bound the blast radius): the **`continues` signer = Foundation** rule
(§1.3, §5) is about *trust of the edge*, not *obligation to enforce*, and stands regardless of B.

---

## 5. The `continues` ceremony

Still undesigned and load-bearing ([0004](../decisions/0004-no-identity-continuity-across-rotation.md),
[0005](../decisions/0005-custody-managed-principals.md)). The schema question is solved — 0004's
decision adopts a `continues` edge that is a **Foundation-issued attestation signed by the stable,
custody-held issuer key**, which breaks the 0004↔0005 loop (0005 records the loop: a `continues` edge
wants a signer that outlives the rotated key → a stable principal → 0005, blocked on 0004) from the
0004 side — for **signing** (no dependency on a stable *agent* principal to produce the signature; the
authority dependency is a separate matter, §5.2). The cost moved from schema to **ceremony**: *how does the Foundation determine that B
continues A?* This section drafts it, using the 2026-08-31 rotation as the concrete test case.

### 5.1 The concrete pair

- **A (predecessor):** `c14094ea7efb…` — handle relabelled `energy@italy-geiant-revoked-c14094ea`,
  revoked 2026-08-31 (key exposed in a public repo), 8 breadcrumbs + 2 epochs orphaned, cert
  `960151d5…`.
- **B (successor):** `d3caa6f17f02…` — handle `energy@italy-geiant`, live, cert `72fdba84…`, chain
  starting near block 0.

A correct ceremony issues one Foundation-signed `continues` edge whose **subject is B** and whose
**target is A's identity anchor**, so a verifier reconstructing B's history can reach A's orphaned
chain.

### 5.2 The crux — A's key is compromised

The naïve ceremony ("the operator proves control of both A and B by signing with each") **fails for
exactly the rotation case that matters**: A is being continued *because* its key leaked, so a
signature from A proves nothing — the attacker holds A too. **`[FLAG]` The continuity of a
compromised-key rotation cannot be self-proven by A.** It must rest on an authority that is *stable
across the rotation and independent of A's key*. That authority is the operator's custody/principal
identity — which is precisely what [0005](../decisions/0005-custody-managed-principals.md) formalizes.

**`continues` therefore depends on [0005](../decisions/0005-custody-managed-principals.md) — for
authority, not for signing.** State this dependency plainly, because it is easy to miss and it
**partially walks back the P0.4 reasoning** in
[0004](../decisions/0004-no-identity-continuity-across-rotation.md). P0.4 held that a
**Foundation-signed** `continues` edge "breaks the 0004↔0005 loop from the 0004 side," so 0004 could
be resolved without 0005 first. That is true **only of the signing dependency**: the *issuer key*
signs the edge, so we do not need a stable *agent* principal to produce the signature. But the edge
asserts a claim — "B continues A" — and **establishing the authority entitled to make that claim,
when A's key is compromised, still requires the stable custody identity 0005 defines.** So:

- **Signing dependency on 0005: broken** (issuer key signs). ✅ — as P0.4 said.
- **Authority dependency on 0005: not broken.** The Foundation can *sign* an unsubstantiated claim,
  but it must not *issue* one; substantiating "B may continue A" for a compromised A rests on custody
  (§5.3.2), i.e. 0005.

This spec does not let 0004 and 0005 quietly disagree: **the loop is broken for issuance mechanics,
not for the trust model.** Foundation-signing lets a *first* `continues` edge be issued before 0005
lands **only** to the extent the authority in §5.3.2 can be established by weaker interim evidence
(existing issuance records, out-of-band operator verification); a robust, non-interim ceremony is
0005-anchored. The ceremony below is explicitly that interim.

### 5.3 Drafted ceremony

The Foundation issues a `continues` edge only after **all** of:

1. **Control of B.** The operator signs a continuation request (a challenge nonce) with **B's**
   secret key. Proves B is the operator's, live, and consenting. (Safe: B is not compromised.)
2. **Authority over A.** The operator demonstrates, **out of band and not via A's key**, that they
   are the party entitled to speak for A's lineage. Acceptable evidence, in descending strength:
   (a) a pre-existing custody/hand-off record naming A's controller (the 0005 direction); (b) the
   Foundation's own issuance records showing it issued/attested A to this operator; (c) an
   out-of-band operator-identity verification (the human/entity — e.g. the Ulissy custody contact).
   `[OPEN]` which of these the Foundation **requires** vs **accepts** is unresolved and is the
   ceremony's hardest question.
3. **A is genuinely retired.** The Foundation confirms A is revoked in the enforcement index
   (§3) — a `continues` into a *live* A would be a fork, not a rotation.
4. **Anti-fork uniqueness.** The Foundation confirms **no other `continues` edge already targets A**.
   `[FLAG]` At most one successor per predecessor. Without this, two parties could each claim to
   continue A and split its reputation history. Enforcing uniqueness is a Foundation-side ledger
   check at issuance, not a schema constraint (the schema cannot see other edges — §3 asymmetry
   again).

On success the Foundation emits, signed by the **issuer key**:

```jsonc
{ "schema": "cgr.attestation.v4",
  "subject_key": "<B agent_pk>",
  "issuer": "gns-foundation",
  "relates_to": [ { "type": "continues",
                    "target": { "kind": "delegation_cert",
                                "hash_alg": "sha-256",
                                "hash": "<A cert_hash — runtime SHA-256, §1.1>" } } ],
  "decision_date": "<date the continuity was determined>",
  "recorded_at":   "<issuance time>",
  "backfilled":    false
  /* … remaining required v4 fields … */ }
```

`[OPEN]` **target anchor: cert vs attestation vs agent_pk.** §5.3 targets A's `cert_hash`. The
alternative is to target a subject *attestation* fingerprint, or to introduce an agent-identity
anchor. `cert_hash` is concrete and already content-addressed, but it binds continuity to a
*certificate*, not an *identity* — if A had multiple certs (it did: `960151d5` revoked and `0b2796c1`
noted in the rotation), which cert anchors the lineage? Leaning: target the **revoked identity's
active cert at revocation time**, but this needs deciding before issuance.

`[OPEN]` **what `continues` transfers.** This spec defines the *link*, not its *consequences*. Does B
inherit A's trust score / tier / epoch history, or merely gain a navigable pointer to it? 0004 frames
rotation-punitiveness as the problem, which argues for inheritance — but inheritance of a
compromised agent's accrued trust is itself a risk. Explicitly out of scope here and **must** be
decided (it interacts with the scoring pipeline, not just the wire format).

---

## 6. Open questions (consolidated)

1. `target.hash` for delegation-cert targets depends on fixing `geiant#10` (two disagreeing cert-hash
   functions) — blocking for any cert-targeting edge. (§1.1)
2. `relates_to` single object vs array. (§1.1) — spec picks array.
3. ~~Cycle handling per type.~~ **RESOLVED (§1.3, P1.2)** — governing principle
   (Lineage-Degrades, Validity-Fails-Closed): `continues` cycle degrades with a distinct
   `anomaly_cycle` signal; `supersedes`/`revokes` cycle rejects; a mixed cycle containing any
   validity-affecting edge rejects.
4. ~~Traversal depth minimum.~~ **RESOLVED (§1.3, P1.2)** — minimum **64**, with the reasoning
   recorded in-spec; on-bound behaviour follows the governing principle (`continues` →
   `truncated_depth`; `supersedes`/`revokes` → reject).
5. ~~`grounding-class`: infer vs boolean; set members.~~ **RESOLVED (§2.2, P1.2)** — **infer from
   `dimension`** against the closed, normative `GROUNDING_DIMENSIONS = { "grounding" }` (pinned in
   §2.2 from the grounding model). Grounding is orthogonal to the capability domain, not exclusive
   with it.
6. Temporal-field overlap: relationship between `as_of`/`last_resolved_at` and
   `decision_date`/`recorded_at`. (§2.2)
7. `domain` vocabulary fixed vs open (0002 Q); relation vocabulary is closed (§1.3, §2.5).
8. Edge form of un-revoke and future-dated revocation. (§3)
9. **All of §4** — 0006 Question B, and its conformance sub-question (0006 Open Q2).
10. `continues` ceremony: which authority evidence is *required* (§5.3.2); target anchor cert vs
    identity (§5.3); what `continues` transfers to the successor (§5.3). The last is a scoring-pipeline
    decision, not a wire one.

---

## 7. What this spec deliberately does not do

- No implementation, no `ACCEPTED_SCHEMAS` edits, no golden-fixture regeneration (P1.3 per the
  roadmap; the `v3` golden MUST NOT be mutated — a **new** `v4` golden is added).
- Does not resolve 0006 Question B (§4) or the ceremony's authority model (§5) — both are flagged for
  their owners.
- Does not decide what `continues` transfers (§5) — that is a scoring-pipeline change beyond the wire
  format.
