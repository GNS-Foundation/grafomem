---
status: proposed
record_date: 2026-09-06
corrected_date: 2026-09-06
provenance: raised-from-implementation — surfaced while scoping B3's minimum attestable disposition record for an AML alert (Ulissy-s-r-l/eu-governed-agent, ADR-0008), and confirmed independently the same day against the TrueForge agent harness and cgr.attestation.v4. Gap 3 CORRECTED TWICE the same day (2026-09-06): first after reading the GNS server (gap 3 overstated as "no namespace"), then again after reading the Dart client (`gns_browser`) — both prior versions wrongly assumed a thin person layer from server-only evidence (see "Corrections to gap 3"). Separately, the AMLR Art. 18 citation was corrected the same day after primary-source verification (Art. 18 = "Outsourcing"; the named-person-decides claim re-cited to Art. 11 + Recital 38 + AI Act Art. 14) — see "Correction — AMLR Art. 18 citation."
scope: (standard) cgr.attestation.v4 signed body, its signature model (§0, §2), and the verifiability_tag vocabulary (§2.2); (implementation) the grafomem CGR read surface and capture path, the GNS identity layer server-side (gns-backend `/identities` + `/aliases`, `records`/`aliases`, proof-of-trajectory `/v1/verify`) AND client-side (the `gns_browser` Dart self-custody client — keygen, Keychain/Keystore custody, `grafomem.hitl.approval.v1` signing, enrollment), and geiant `agent_registry` — the person-capable identity plumbing gap 3 concerns — and, as corroboration, the TrueForge harness session_event schema.
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

## Corrections to gap 3 — read this first (gap 3 has been corrected TWICE)

Gap 3 has now been rewritten **twice**, and both prior versions were wrong **for the same reason**:
they described the person-identity layer as thin, and that was **an artefact of reading only the
server**. The client half of a self-custody identity system was never examined until the second
correction. The pattern matters more than either specific error: **a finding about a distributed
system was derived from one side of it, and stated with more confidence than one-sided evidence
supported — twice.** Version 1 (at creation): "no natural-person identity namespace." Version 2
(**Correction 1**, below): "no *accountable, verified* person identity" — better, but still assumed the
person layer was thin because it too read only `gns-backend`. Version 3 (**Correction 2**, below, and
the current gap-3 body) restates it after reading the **Dart client**: the person layer is **not thin —
it is unassured and unwired**. Both corrections are kept visible rather than quietly settling on a third
version.

### Correction 1 (2026-09-06) — gap 3 was too strong ("no namespace")

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

### Correction 2 (2026-09-06, after reading the Dart client) — gap 3, second revision

Correction 1 still read only the **server**. A read-only investigation of the **Dart/Flutter client**
(`gns_browser`, HEAD `6ec2d01`, active) showed the human-identity workflow is **genuinely split**, and
the client holds the sovereign half — so "the person layer is thin" was wrong a **second** time. **What
is actually built on the client** (record as built; do not re-derive):

- **Self-custody.** `gns_browser` generates an **Ed25519 + X25519** keypair **on device**
  (`identity_keypair.dart:30-55`) and stores the private key in **`flutter_secure_storage`** — iOS
  Keychain / Android EncryptedSharedPreferences (`secure_storage.dart:23-24,164-166`). **The private
  key never crosses the wire** (only public key + signatures are sent).
- **A human-approval signing primitive, domain-separated against blind-signing.** `signBytesToHex()`
  **refuses to sign** any payload lacking the prefix **`grafomem.hitl.approval.v1:`**
  (`identity_wallet.dart:179-202`). **The domain tag is already namespaced for grafomem** — the
  connection between a person's self-custodied approval signature and grafomem HITL was **contemplated
  and left unfinished.**
- **An owner→agent attestation** *"signed by the human owner"* (`agent_identity.dart:67`).
- **A real enrollment flow** — handle **reserve** then **claim**, signed client-side, with the server
  storing `pubkey / handle / pot_proof / signature` and verifying the signature (`identity_wallet.dart`
  → `POST /aliases/{h}/reserve`, `PUT /aliases/{h}`; `aliases.ts:156-271`).

So self-custody and human-approval-signing — the parts Correction 1 still implied had to be built — are
**built**. Gap 3 is therefore restated a second time below (**"not thin — unassured and unwired"**),
what-is-built is moved out of the gap, and a **caveat** and an **open question** (both below) are added.
The record's **conclusion is still unchanged**: the standard expresses one actor; a defensible approval
needs two.

## Correction — AMLR Art. 18 citation (2026-09-06)

The original record cited **"AMLR Art. 18 — responsibility is non-transferable"** as the ground for *both*
claims it makes: (i) accountability cannot pass to the agent, and (ii) a named natural person must decide
and sign. **Verified against primary source, that citation was carrying more than it should.** AMLR
**Art. 18 is titled "Outsourcing"**; its operative sentence is *"The obliged entity shall remain fully
liable for any action … connected to the outsourced tasks that are carried out by service providers"*
(EUR-Lex Reg. (EU) 2024/1624; amlr.eu/article-18-outsourcing). It contains **no natural-person signature
provision.**

- **Claim (i) survives on Art. 18** — an agent preparing dispositions is functionally an outsourced /
  automated task, and Art. 18 keeps liability with the obliged entity, so accountability cannot transfer
  to the agent. This is the **outsourcing lens**, and it is sound.
- **Claim (ii) does not rest on Art. 18** — the "named natural person decides/signs" claim rests on
  **AMLR Art. 11 (Compliance functions)** — the management body appoints a compliance officer *"responsible
  for the policies, procedures and controls in the day-to-day operation"* — and **Recital 38** (*"responsibility
  … should rest ultimately with the management body"*), plus **AI Act (Reg. (EU) 2024/1689) Art. 14 (Human
  oversight)** (a natural person can *"disregard, override or reverse the output"*). **No** EU provision
  mandates a *signature technology* for dispositions; the requirement is accountability + oversight, and a
  signature is the *means of evidencing* it. The body above (Context and gaps 2/3) has been re-cited
  accordingly.

**Propagation note (recorded, per the discipline of the other corrections).** This mis-citation entered
through a **research report**, was repeated into this record and into `Ulissy-s-r-l/eu-governed-agent`'s
roadmap **without being checked against source**, and would have gone into the pitch — it is *load-bearing
in the pitch, not just here*. It is the **third instance this week** of a finding taken from a single
source and propagated before verification: cf. [[0008]]'s "two actors" (a one-side repo sweep, corrected
to three) and this record's gap 3 (corrected twice from **server-only** reading before the client was
examined). The pattern — **confidence exceeding the breadth of the evidence, then propagated** — is the
thing to notice, not the individual error. Fixed here and in the roadmap; verified against EUR-Lex /
amlr.eu / the AI Act text before correcting.

## Context — how it was found

Scoping B3's **minimum attestable disposition record** for an AML alert reduced the requirement to a
single shape:

> **agent prepared X, human Y decided Z under authority A, and Y signed the whole.**

This shape is not a design preference; it is what the governing instruments require:

- **AMLR (Regulation (EU) 2024/1624) — a named, designated natural person is accountable.** The
  compliance function is a **named person**: the management body appoints a member responsible for
  compliance and **"a compliance officer … responsible for the policies, procedures and controls in the
  day-to-day operation"** (**Art. 11, Compliance functions**), and *"responsibility … should rest
  ultimately with the management body"* (**Recital 38**). So the accountable decision-maker is a
  specific natural person, not the tool — the record must make that person **identifiable and bound to
  the disposition**. *(See the citation correction below: this claim rests on Art. 11 / Recital 38, not
  on Art. 18.)*
- **AMLR Art. 18 (Outsourcing) — accountability cannot be transferred to the agent.** *"The obliged
  entity shall remain fully liable for any action … connected to the outsourced tasks that are carried
  out by service providers."* An agent that prepares dispositions is functionally an **outsourced /
  automated task**, so Art. 18 keeps the liability with the obliged entity and its compliance officer —
  it **bars transfer of accountability to the agent**. Art. 18 does **not** mandate a human signature;
  it establishes non-transfer, which is why the *agent prepares, the human decides* split is required.
- **AI Act (Regulation (EU) 2024/1689) Art. 14 — human oversight.** A natural person must be able to
  **oversee and intervene** — high-risk AI must let a deployer *"disregard, override or reverse the
  output"* and *"intervene … or interrupt"* — and the record must show the intervention was
  **exercised**, not presumed. A rubber-stamp that cannot be distinguished from a genuine review does
  not satisfy this.

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
accountability rests on the obliged entity's designated compliance officer (AMLR Art. 11; Recital 38),
and cannot be transferred to an automated/outsourced actor (Art. 18), yet the record binds to the
emitter instead.

**3. The person layer is not thin — it is _unassured_ and _unwired_.** *(Restated a **second** time
2026-09-06 — see "Corrections to gap 3." v1: "no namespace." v2: "no accountable, verified person
identity." v3, here: the person layer is substantial but unassured and unwired. Both earlier versions
read only the server; this one reads the client too.)*

**What is built (recorded as built — do not re-derive; details in Correction 2 above):** a real
self-custody person-identity **client** exists — `gns_browser` generates Ed25519+X25519 **on device**
and keeps the private key in Keychain/Keystore (`identity_keypair.dart:30-55`,
`secure_storage.dart:23-24`), signs with a **domain-separated human-approval primitive** that refuses
any payload lacking `grafomem.hitl.approval.v1:` (`identity_wallet.dart:179-202` — a tag **already
namespaced for grafomem**), carries an **owner→agent attestation signed by the human owner**
(`agent_identity.dart:67`), and runs a **real enrollment flow** (handle reserve/claim, signed
client-side; server stores pubkey/handle/pot_proof/signature and verifies). So **self-custody and
human-approval-signing are built**, not missing.

**What survives — the surviving gaps.** The person layer cannot yet carry accountability for four
reasons:

- **(a) No real-world identity assurance binding a key to a _named_ person.** There is **no KYC and no
  IDV anywhere** (client or server). `proof_of_humanity` is **proof-of-_trajectory_**
  (`gns-backend/src/api/verify.ts`, *"rather than biometrics"*): a **sybil-resistance** scheme that
  proves *a key-holding device has moved and accrued trust over time*, **not that a specific named human
  is present or is who they claim to be**. The AMLR accountability model (the named compliance officer,
  Art. 11 / Recital 38) needs a positive binding of key → *named* person; GNS
  provides key → *pseudonymous, movement-attested* handle. `human` is still additionally derived by
  **absence** server-side (`identities.ts:107-108` — not-in-agent-table ⇒ human, no stored
  `subject_type`), and the alias `verified` flag is proof-of-trust, not identity assurance.
- **(b) The human-approval signature is not wired into the CGR mint.** The v4 attestation is
  Foundation-signed and carries **no slot** for the client's `grafomem.hitl.approval.v1` signature
  (§0, §2.4; gap 2). The two halves exist but are **not connected** — the grafomem-named domain tag is
  evidence the connection was contemplated and never completed.
- **(c) No role-tenure lifecycle.** Custody exists, but *"valid then, not authorized now"* (an MLRO
  leaving a role while their past signatures stay valid and attributable) is not modeled; revocation is
  an agent-level denylist (`agent_registry.revoked_at`).
- **(d) The server still types `human` by absence** — a rich client does not change that there is no
  **positive, typed, verified** person assertion anywhere.

**The caveat that must ride with this (bears on any design choice).** `gns_browser` is a **consumer
geolocation and gamification app**; its identity model is engineered for **pseudonymous
sybil-resistance**, which is the **wrong assurance model for AML**. **Adopt the _mechanism_** —
self-custody keys and domain-separated approval signing — **not the identity _semantics_.**
**Proof-of-trajectory cannot be back-filled into proof-of-identity**: accruing movement history never
becomes a vetted named person, so real-world assurance (a) is net-new regardless of how much of the
client is reused.

**Precondition (unchanged, and sharpened):** converting **`human`-by-absence into a positive, verified
assertion** — and adding real-world identity assurance (a) — is a precondition for closing this record
**regardless of which design option is later chosen**. No option that leaves accountability resting on a
derivation or on proof-of-trajectory can satisfy the AMLR accountability model (Art. 11 / Recital 38,
with Art. 18 barring transfer to the agent).

Note the adjacency to [[0008]]: that record found there is no shared *data path* for agent identity
across the systems that need it; this one finds a **substantial person-identity client** that exists but
**cannot yet carry accountability** (unassured, unwired) — the same under-built identity substrate, seen
at the person layer (see **Shared framing with 0008** below).

**What a natural-person principal must satisfy that an agent principal need not.** This bears on any
design, so it is recorded here rather than deferred:

- **Self-custody of the signing key.** An agent's key is **system-held** (`GEIANT_AGENT_SK` in
  Railway env, geiant `setup-agent.ts`) — correct for an agent, because the system *is* the agent. An
  MLRO's key **cannot** be system-held: whoever holds the secret can forge the approval, so a
  system-custodied signature **proves nothing about the human's decision**. **This requirement is
  already met by `gns_browser`** (on-device keygen, Keychain/Keystore custody, key never leaves the
  device) — it is a client to adopt, not a mechanism to build (gap 3(a)/(b)). The **governed-agent**
  path, by contrast, does not self-custody.
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
scoped concretely (AMLR Art. 11 + Recital 38 accountability, Art. 18 non-transfer of liability, AI Act
Art. 14 oversight). Per **ADR-0008** (B3 is the best *client* of a
general ledger; anything the ledger cannot express becomes a Foundation record, not a product-side
workaround), the fix belongs **upstream in the standard** — a general two-actor / natural-person
identity capability — **not as an AML bolt-on in B3's write path.** Solving it in B3 would be exactly
the "vendor format with a foundation logo" that ADR-0005 and ADR-0008 exist to prevent.

## What this record does not do

It does **not** propose a solution. Whether the answer is a second signature slot, an approver
principal field, a natural-person identity namespace with its own registry and delegation model, or
some composition of these — and how any of it interacts with [[0003]]'s ephemerality and [[0008]]'s
missing data path — is a **later decision**. This record's job is narrower and prior to all of that:

> **The standard models one actor where the regulation requires two. The prerequisite for closing the
> gap is not a person _namespace_ — a self-custody person client already exists — but an _accountable,
> verified, and wired_ person identity: real-world assurance binding a key to a named person, and that
> person's approval signature bound into the attestation.**

## Open questions

- **Identity-layer prerequisite (blocks any fix for gaps 1–2):** what is the natural-person identity
  namespace — a registry, a key model, a delegation/authority model — and how does it relate to
  `agent_pk`, to `delegation_certificates`, and to [[0003]]'s ephemeral principals? Does it share the
  [[0008]] identity plumbing or is it a separate layer? *(Partly answered by the client investigation: a
  self-custody person client exists (`gns_browser`); the open part is real-world assurance (gap 3(a))
  and wiring to the mint (gap 3(b)).)*
- **iCloud Keychain sync of the signing key (open question, not a defect).** `gns_browser`'s
  `flutter_secure_storage` is configured `synchronizable: true` (`secure_storage.dart:53-58`), so the
  private key **propagates across a user's Apple devices via iCloud Keychain**. Appropriate for a
  consumer wallet; **an open question for a signing key whose entire purpose is that exactly one person
  controls it** — cloud propagation widens the custody surface of an accountability key. To be resolved
  by whoever adopts the mechanism (gap 3), not asserted here as wrong.
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
