---
status: proposed
decision_date: "—"
record_date: 2026-08-31
provenance: surfaced during the geiant .env.agent exposure response (2026-08-31)
scope: cgr.attestation.v3; geiant agent identity (reference implementation)
---

# 0004 — No identity-continuity mechanism across rotation

- **Status:** **Proposed** (open — and the third instance of one underlying gap)
- **Record date:** 2026-08-31

## Context

Rotating a compromised agent key produces a **new, unrelated identity**. Nothing in the schema
expresses "agent B is the continuation of agent A."

Concretely, from the GEIANT reference implementation: agent `c14094ea…bc04` holds 8 breadcrumbs and
2 epochs signed under certificate `960151d5…`, revoked 2026-08-31. A replacement agent starts at
`block_index 0` with an empty chain. The history is not transferred, not linked, and not marked
superseded — it is **orphaned**. A verifier cannot distinguish "a new agent" from "the same operator,
rotated key."

The same gap exists one level up. The Foundation key-custody runbook already records that
`/v1/cgr/rotations` **cannot** serve issuer rotation — that endpoint is the subject/agent-identity
continuity chain, and `resolve_identities` folds an agent's key-rotation links into per-anchor
chains. Issuer rotation is a re-pin ceremony, not an API call. So **neither the agent level nor the
issuer level can express continuity**, for different reasons, and neither has a schema construct for
it.

## This is the third instance of one gap

| Record | Cannot express |
|---|---|
| [0001](0001-cgr-grounding-dimension-additive-vs-schema-bump.md) | a grounding dimension without a schema bump |
| [0002](0002-cgr-governance-domain-and-backfill.md) | governance domain; backfill / temporal provenance |
| **0004** (this) | identity continuity across rotation (supersedes / continues) |

0001 already notes that 0002 is "a second instance of the same tradeoff… One principle should
resolve both." This record is the third, and it sharpens what the shared principle is.

**The recurring failure is not three separate missing fields. It is that the schema has no general
mechanism for expressing a relation between attestations** — no way to say *this supersedes that*,
*this continues that*, *this corrects that*. Each gap has so far been met with a bespoke proposal for
the specific field that happened to be missing, which is why the same shape keeps recurring under
different names. A grounding dimension, a backfill marker, and a rotation link are three faces of
one absent primitive: **a typed, signed edge from one attestation to another.**

Treating them separately guarantees a fourth instance.

## Decision (open — two options, not resolved here)

**(a) A generic relation edge** in the attestation schema:

```
relates_to: { type: supersedes | continues | corrects, target: <attestation-or-cert-hash> }
```

Signed, so the relation is tamper-evident and the claim is made by the party entitled to make it.
This closes 0001, 0002 and 0004 with one construct, and gives the fourth instance somewhere to land.
More design work now — relation types need a governed vocabulary, and verifiers need rules for
traversing edges (depth limits, cycle handling, whether an unrecognised relation type is fatal or
ignorable).

**(b) A rotation-specific continuity record**, signed by the **outgoing** key, naming the incoming
`agent_pk`. Narrow, shippable quickly, solves only this record. Note it interacts with
[0003](0003-principal-identity-is-not-stable.md): if the outgoing key is compromised — the usual
reason to rotate — a signature from it is exactly the wrong authority, so (b) likely requires a
principal-level countersignature and therefore a stable principal.

**This record does not pick one.** It argues that (a) and (b) should not be evaluated in isolation
from 0001 and 0002 — the three should be decided together, by one principle, rather than separately
a fourth time.

## Consequences

- **Rotation is operationally punitive.** It destroys accumulated trust score, tier, and chain
  history, creating pressure *not* to rotate — precisely backwards for a security control. In the
  incident that surfaced this, "revoke but do not rotate" was the path of least resistance, and the
  schema is part of why.
- Revocation and rotation are separable today only because revocation was bolted onto an existing
  column (`delegation_certificates.revoked_at`, enforced as of GNS-Foundation/geiant#9). Continuity
  has no equivalent column to bolt onto.
- Any verifier reconstructing an agent's history across a rotation boundary must be told about the
  link out of band, which is not a property a standard should require.

## Open questions

1. Does the relation edge belong in the **signed body** (tamper-evident) or the envelope (advisory)?
   The same question 0002 raises for `decision_date`; the answer should be the same one.
2. Should relation types be a **fixed enum** or an **open, conformance-marked vocabulary**? Again
   the same axis as 0002's governance-domain question.
3. What does a verifier do with an **unrecognised** relation type — ignore, warn, or fail?
4. Depth and cycle rules for traversing chained relations.
5. Sequencing: is (a) a `cgr.attestation.v4` concern, or expressible additively under 0001's path A?
