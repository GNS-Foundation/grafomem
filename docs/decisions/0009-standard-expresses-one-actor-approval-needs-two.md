---
status: proposed
record_date: 2026-09-06
corrected_date: 2026-09-06
provenance: raised-from-implementation — surfaced while scoping B3's minimum attestable disposition record for an AML alert (Ulissy-s-r-l/eu-governed-agent, ADR-0008), and confirmed independently the same day against the TrueForge agent harness and cgr.attestation.v4. CORRECTED the same day (2026-09-06) after a read-only investigation of the GNS identity layer found gap 3 was overstated (see "Correction").
scope: (standard) cgr.attestation.v4 signed body, its signature model (§0, §2), and the verifiability_tag vocabulary (§2.2); (implementation) the grafomem CGR read surface and capture path, the GNS identity layer (gns-backend `/identities` + `/aliases`, the `records`/`aliases` tables) and geiant `agent_registry` — the person-capable identity plumbing gap 3 concerns — and, as corroboration, the TrueForge harness session_event schema.
---

# 0009 — the standard expresses one actor; a defensible approval needs two

- **Status:** **Proposed** 2026-09-06 — this states the problem; it does **not** resolve it. It records
  a category-level gap: the standard models **one actor** (the agent, signed by the Foundation issuer)
  where the regulation a governed AML disposition must satisfy requires **two** (an agent that prepares
  and a named natural person who decides and signs).
- **Record date:** 2026-09-06
- **Relates to:** [[0003]] (principal identity is not stable — why there is no natural-person key to
  sign as), [[0008]] (identity continuity has no shared data path — the adjacent missing-identity
  plumbing, one layer down), [[0002]] (governance domain + backfill — origin of `verifiability_tag`
  and the temporal fields discussed in gap 5), and `cgr-attestation-v4-spec.md` §0/§2 (the signed body
  and its single signature). Product-side motivation: `Ulissy-s-r-l/eu-governed-agent` ADR-0008 (B3 is
  the best client of a general ledger) and ADR-0003 (DORA Art. 30(3)).

## Correction (2026-09-06) — read this first

The original gap 3 (below, restated) claimed **"no natural-person identity namespace."** A same-day,
read-only investigation of the GNS identity layer showed **that was too strong.** GNS **has a
person-capable identity layer**: `GET /identities/:pk` derives `subject_type` with **`human` as the
default** (`gns-backend/src/api/identities.ts:107-108`); handles are **person-claimable** and verified
against a **client-held `pk_root`** (`aliases.ts:220`); and the OAuth login path already mints
`gns_subject_type: 'human'` as an id-token claim (`oauth.ts:733,748`). **Self-custody is a first-class
path.** The record's **conclusion is unchanged** — the standard still expresses one actor, and a
defensible approval still needs two — but gap 3 is **restated** below from "no namespace" to "no
**accountable, verified** person identity, not wired to the accountable-signature model," which is the
sharper and correct finding. Two subsections are added — the **precondition** this exposes and **what a
natural-person principal needs that an agent does not** — and a **shared-framing** section ties this
record to [[0008]]. Corrected visibly, not silently, per the discipline of the 0008 rewrite.

## Context — how it was found

Scoping B3's **minimum attestable disposition record** for an AML alert reduced the requirement to a
single shape:

> **agent prepared X, human Y decided Z under authority A, and Y signed the whole.**

This shape is not a design preference; it is what the governing instruments require:

- **AMLR (Regulation (EU) 2024/1624) Art. 18 — responsibility is non-transferable.** The obliged
  entity, and in practice a **named natural person** (the MLRO or a delegate), remains accountable for
  the disposition. The agent **prepares**; the human **decides**. A defensible record must therefore
  make the **human decision-maker identifiable and bound to the decision** — not the tool, not the
  vendor.
- **AI Act Art. 14 — human oversight.** A natural person must be able to **oversee and intervene**
  (approve, modify, override, escalate), and the record must show the intervention was **exercised**,
  not presumed. A rubber-stamp that cannot be distinguished from a genuine review does not satisfy this.

**Confirmed independently in two unrelated systems on the same day (2026-09-06):**

1. **TrueForge** (an MIT agent harness evaluated as a possible B3 foundation): its approval event
   persists `status` and an optional `reason`, but **no actor** — there is no field recording *who*
   approved. "The agent did X, approved by Y, at step N" is not expressible; N and X are
   reconstructable by joining on `tool_call_id`, but **Y is not persisted anywhere.**
2. **`cgr.attestation.v4`**: the signed body models the **agent** (`subject_key`) and the **issuer**
   (`issuer` / `issuer_key_id`). There is **no approver principal**, and the wire carries **exactly one
   signature slot** — the Foundation issuer's.

Two codebases with nothing in common, the **same absence**. That is what makes this a **category-level
gap in the model**, not an oversight in either implementation. The scoping is the finding; this record
is its promotion to a Foundation decision.

## The gaps — six, led by the three irreducible ones

### The irreducible three

**1. No approver principal.** `v4` models the **agent** (`subject_key`) and the **issuer**
(`issuer`/`issuer_key_id`). There is **no natural-person approver field**. The relation edge
(`relates_to`, §1) links attestations and delegation certs — lineage between records — not a human
decider to a decision. So **"approved by Y" is inexpressible** in the standard as it stands.

**2. No human non-repudiation.** The wire is flat: the signed body is every top-level key except the
two envelope keys, and the signature is Ed25519 over that body — **one signature, the Foundation
issuer's** (§0, §2.4). A supervisor can verify **the Foundation issued the record**; they cannot verify
**the MLRO signed it**. The single signer is structurally the **wrong signer for accountability** —
accountability under Art. 18 rests on the natural person, and the record binds to the emitter instead.

**3. No _accountable, verified_ natural-person identity — and the accountable-signature model is not
wired to the person-capable layer that exists.** *(Restated 2026-09-06 — see the Correction. The
original claim, "no natural-person identity namespace," was too strong: a person-capable layer exists.)*

A person-capable identity layer **does** exist in GNS: `GET /identities/:pk` derives `subject_type`
with **`human` as the default** (`gns-backend/src/api/identities.ts:107-108`), handles are
**person-claimable** and verified against a **client-held `pk_root`** (`aliases.ts:220`), and the OAuth
path already mints `gns_subject_type: 'human'` (`oauth.ts:733,748`). **Self-custody is a first-class
path.** So the prerequisite for gaps 1 and 2 is **closer than first written** — but two things it needs
for accountability are missing, and these are the sharper finding:

- **`human` is derived by _absence_, not asserted.** An identity is `human` simply because its pubkey
  is **not** found in the agent table (`identities.ts:107-108`); there is **no stored `subject_type`
  column**. Accountability that rests on *"we didn't find this key in the agent table"* is not
  accountability — it is a default, and a positive **verified** assertion is what Art. 18 requires.
- **`verified` is proof-of-trust, not identity assurance.** The alias `verified` flag attests
  proof-of-trust over the handle claim; it proves *a handle controls a key*, **not which real person
  holds it**. Art. 18 needs a **positive, verified binding of key → named person** (an MLRO the
  supervisor can confirm), which GNS does not provide.

**And the accountable-signature model is not wired to this layer at all.** The v4 mint signs with the
Foundation issuer key (gap 2); nothing binds a GNS `human` identity's self-custodied key to a
disposition. `agent_pk` is the **agent's** key; `delegation_certificates` bind a **principal to an
agent**; per [[0003]] principals are **ephemeral** (minted in memory, discarded). So even with a
person-capable registry present, **there is still no accountable analyst/MLRO identity bound to a
signed decision.**

**Precondition (record it explicitly):** converting **`human`-by-absence into a positive, verified
assertion** is a precondition for closing this record **regardless of which design option is later
chosen**. No option that leaves accountability resting on a derivation can satisfy Art. 18.

Note the adjacency to [[0008]]: that record found there is no shared *data path* for agent identity
across the systems that need it; this one finds the person-capable layer that exists **cannot yet carry
accountability** — the same under-built identity substrate, seen at the person layer (see **Shared
framing with 0008** below).

**What a natural-person principal must satisfy that an agent principal need not.** This bears on any
design, so it is recorded here rather than deferred:

- **Self-custody of the signing key.** An agent's key is **system-held** (`GEIANT_AGENT_SK` in
  Railway env, geiant `setup-agent.ts`) — correct for an agent, because the system *is* the agent. An
  MLRO's key **cannot** be system-held: whoever holds the secret can forge the approval, so a
  system-custodied signature **proves nothing about the human's decision**. The ecosystem *can* do
  self-custody (GNS BYOK / client-signed alias claims), but the governed-agent path does not use it.
- **A role-tenure lifecycle, not a denylist.** Agent revocation is outright deny
  (`agent_registry.revoked_at`). A person leaving a role is different: their **past signatures must
  remain valid and attributable** (they were accountable then) while their **authority to sign new
  dispositions ends** — *"valid then, not authorized now."* An outright denylist cannot express that.
- **External verifiability, not internal consistency.** An agent identity need only be internally
  consistent (a pubkey the system recognizes). A person identity must be verifiable **to a supervisor**
  — they must confirm the key belongs to the **specific named accountable person**, not merely that
  some registered, `human`-by-default key signed.

### The other three

**4. No disposition-content schema.** The disposition's substance — alert reference, subject-under-
review, the digest of the evidence the agent actually saw, its conclusion, the outcome
(file STR / no-file / escalate), and the human-intervention flag — has **no `v4` home**. This is the
**unbuilt B2 workstream** ("AML disposition evidence schema: what the agent must record for a narrative
to be defensible under AMLR"), noted here so the scoping is complete; it is a product-adjacent gap, not
part of the irreducible identity problem.

**5. `verifiability_tag`'s axis conflation.** `verifiability_tag` (from [[0002]]) has two values —
`judgment` (moves a score) and `rule` (recorded, non-scoring governance record). These conflate two
**independent** axes: **temporal shape** (per-record decision vs pooled aggregate) and **scoring
effect** (scores an agent vs does not). A substantive **human-signed disposition** is a *third* thing
the vocabulary has no value for: it is a single dated decision (per-record in shape) that is not a
governance *rule* and whose point is not to score the agent. Consequently the temporal fields a
disposition needs — `decision_date`, `recorded_at`, `backfilled` — are **absent on pooled aggregates**
(what the read surface mints) and reachable **only by overloading `verifiability_tag: "rule"`**, i.e.
by mis-classifying a substantive decision as a governance rule (and that branch is itself
normative-but-unenforced today). The vocabulary cannot name what a disposition is.

**6. The read surface is structurally the wrong producer.** grafomem's CGR read surface is an
**emitter**: it re-mints a fresh Foundation-signed attestation per read, **persists nothing**, and its
input is *reputation substrate* (captured decisions joined to resolved outcomes), producing **pooled
aggregates**. It therefore has no ordering, no append-only position, and the wrong output type — it
neither ingests nor stores a per-alert disposition. (The capture path plus grafomem's append-only
gcrumbs chain supplies ordering / append-only / tamper-evidence, but still binds to a **system** key,
not the human decider's — so it does not close gaps 1–3 either.)

## Shared framing with 0008 — two relations over one under-built substrate

*(Added 2026-09-06.)* [[0008]] and this record are **not one problem, and not two unrelated ones.**
They are **two relation types over one under-built identity substrate:**

- **[[0008]] needs agent → agent succession** — a record binding one `agent_pk` to its successor across
  rotation. The identities are **agents** whose keys are **system-held** and rotate on compromise.
- **This record (0009) needs person → key → decision** — a record binding a **natural person** to a
  self-custodied key, and that key to an approval.

**Neither is expressible for the same reasons.** The current model **derives actor type rather than
storing it** (`identities.ts:107-108` — `human` by absence), **discards principals** (per [[0003]],
ephemeral and self-vouching), **stores no succession edge** ([[0008]] — no rotation/predecessor record
type anywhere), and **binds no person to a signature** (gaps 1–3 above). Fix the substrate and each
becomes one relation type over it; leave it and neither can be expressed. That is why they should be
weighed **together** — a single design that serves both is worth more than two point fixes — even
though the choice of design is the next decision, not this record's to make.

State this plainly: the gap is **not about AML.** Any **regulated approval workflow** — anywhere a
human must approve, and be accountable for, what an agent prepared — needs the same thing: **"human Y
approved agent's X, signed and reconstructable."** AML is merely the first place the requirement was
scoped concretely (AMLR Art. 18, AI Act Art. 14). Per **ADR-0008** (B3 is the best *client* of a
general ledger; anything the ledger cannot express becomes a Foundation record, not a product-side
workaround), the fix belongs **upstream in the standard** — a general two-actor / natural-person
identity capability — **not as an AML bolt-on in B3's write path.** Solving it in B3 would be exactly
the "vendor format with a foundation logo" that ADR-0005 and ADR-0008 exist to prevent.

## What this record does not do

It does **not** propose a solution. Whether the answer is a second signature slot, an approver
principal field, a natural-person identity namespace with its own registry and delegation model, or
some composition of these — and how any of it interacts with [[0003]]'s ephemerality and [[0008]]'s
missing data path — is a **later decision**. This record's job is narrower and prior to all of that:

> **The standard models one actor where the regulation requires two, and a natural-person identity
> namespace is the prerequisite for closing the gap.**

## Open questions

- **Identity-layer prerequisite (blocks any fix for gaps 1–2):** what is the natural-person identity
  namespace — a registry, a key model, a delegation/authority model — and how does it relate to
  `agent_pk`, to `delegation_certificates`, and to [[0003]]'s ephemeral principals? Does it share the
  [[0008]] identity plumbing or is it a separate layer?
- **Signature model:** does a defensible approval need a **second signature** in the wire (the approver
  co-signs), or a separate approver attestation **linked** to the agent's record — and what does either
  do to the current single-signer, flat-body format (§2.4)?
- **`verifiability_tag`:** does a substantive human-signed decision need a **new value** (a third axis
  member), or is the judgment/rule vocabulary the wrong factoring to begin with (gap 5)?
- **Producing surface:** if the read surface cannot produce this (gap 6), is the capture+gcrumbs path
  the base to extend, or is a distinct disposition surface required — and where does the approver's
  signature enter?
- **Scope confirmation:** is the two-actor requirement truly general across regulated approval
  workflows (as claimed above), or are there governed workflows where a single accountable actor
  suffices — which would change whether this is a core standard capability or an optional profile?
