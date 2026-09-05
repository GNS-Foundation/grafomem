---
status: proposed
record_date: 2026-09-04
provenance: raised-from-implementation — surfaced during the read-only pre-flight of the §5.3 continues-edge ceremony for the c14094ea → d3caa6f1 rotation (2026-09-04); the concrete first case has no CGR substrate on the grafomem read surface
scope: (standard) the CGR continues edge (§1, §5.3) and 0004 rotation-continuity; (implementation) grafomem's read surface + CGR substrate AND geiant's agent_registry / audit-chain — the two systems that jointly need identity continuity
---

# 0008 — identity continuity has no shared data path between the systems that need it

- **Status:** **Proposed** 2026-09-04 — this states the problem; it does not resolve it. It records a
  structural gap found when the `continues` edge was ready to issue and had no valid first case.
- **Record date:** 2026-09-04
- **Relates to:** [[0004]] (no identity continuity across rotation — the motivation), [[0005]]
  (custody-managed principals), [[0006]] (enforcement boundary; label non-strippability), [[0007]]
  (no reverse edge index), and `cgr-attestation-v4-spec.md` §1 / §5.3 (the continues edge + ceremony).

## Context

The CGR attestation's `subject_key` **is** the agent's geiant Ed25519 pubkey, captured at decision
time (Ticket #5 binding; `engine.py` folds by "the IDENTITY ANCHOR of the agent's captured GEIANT
pubkey"). So the two systems **share an identity namespace** — a geiant `agent_pk` is a valid grafomem
CGR `subject_key` value; they are not separate namespaces.

But they do **not** share a data path. A grafomem CGR subject is built from **Ticket-#1 captured
governed decisions joined to resolved outcomes** (`substrate.py`). Geiant agents write **breadcrumbs
to geiant's audit chain**, which populate geiant's `agent_registry` + breadcrumb/rotation chain — a
different store, not grafomem's CGR substrate. **Shared identity, disjoint data.**

**Verified live (2026-09-04), read-only through the decrypting read surface across every
CGR-enabled tenant:** the only subjects served are `*@ulissy` (`gtm-outreach-agent`, `eng-agent`) and
`*@virtualbank` (the Meridian sim: `thin-00/01/02`, `estab-02`). **No geiant agent has CGR
substrate**; `energy@italy-geiant` / `d3caa6f1` is not a subject anywhere on the surface.

## Consequence

The `continues` edge is specified in the CGR attestation (§1) and injected at **grafomem's mint**
(the read surface, Option A) — but the problem it exists to solve, [[0004]]'s
**rotation-punitiveness**, is about **geiant's** orphaned `agent_registry` score and breadcrumb chain.
Those live in geiant, not grafomem.

So a geiant rotation can express continuity on the grafomem read surface **only if that agent also
happens to be a scored grafomem CGR subject** — i.e. only if its governed decisions and resolved
outcomes were captured into grafomem. The motivating concrete case, **`c14094ea → d3caa6f1`, is
not** such a subject, so **the case that motivated the design cannot carry the edge it motivated.**
The edge would be written and then never surface (no attestation is minted for a non-subject).

## What was built is correct — it simply has no first case

This is not a defect in the implementation. The edge store (`cgr.continues.v1` in the identity
store), the mint injection (`build_read_envelope` → signed-body `relates_to`), the one-shot ceremony
(`scripts/cgr_continues_ceremony.py`, four §5.3 preconditions), and the conformance vectors
**CL1/CL2** (continues → delegation_cert, resolvable → `complete`, absent → `truncated_unavailable`)
all work correctly **for grafomem CGR subjects**. They are landed and tested; they lack a valid first
subject on this surface, not correctness.

## Options (stated, not chosen)

1. **A different subject** — issue the first `continues` edge for a **grafomem CGR subject that
   actually rotates** (an Ulissy/Meridian agent with captured decisions + resolved outcomes). This
   exercises the built machinery on the surface it was built for, with no new surface. It does not,
   by itself, serve [[0004]]'s geiant-rotation case.
2. **A different surface** — have **geiant emit its own lineage attestation** (or express continuity
   in its `agent_registry`/chain), where the rotating agents, the rotation chain, and the breadcrumbs
   already live. This puts continuity where the punitiveness actually is, but means a **new emitting
   surface** with its own issuance path and its own deploy cost (grafomem#108: a redeploy is a
   ~25–30 min single-replica outage; a new geiant surface has its own equivalent).
3. **Something else** — e.g. a bridge that projects geiant identity/rotation events into grafomem CGR
   substrate (making geiant agents subjects), or a shared lineage store both surfaces read. Larger;
   named only so the choice is not framed as binary.

## Interaction with the other open questions

Whichever surface issues lineage **inherits the still-open questions**, so this decision should not be
made in isolation from them:

- **Label non-strippability ([[0006]], open):** an emitted lineage/enforcement label must be
  non-strippable on that surface; a new geiant emitting surface would have to solve it there too.
- **The reverse index ([[0007]], open):** `continues`-only lineage does **not** need it (Lineage-
  Degrades), but any surface that also carries `revokes`/`supersedes` does — so a geiant lineage
  surface that grows beyond `continues` re-opens 0007 on the geiant side.
- **What `continues` transfers (§5.3, SPLIT — navigable lineage decided, CGR pooling deferred):** the
  answer is currently framed around grafomem's CGR score. If lineage moves to geiant, "what
  transfers" must be answered for geiant's `agent_registry` score/chain as well — a different score
  in a different system.

## Recommendation on status

Left **open** for a Foundation decision. The immediate engineering reality is recorded: **do not run
the `c14094ea → d3caa6f1` ceremony against the grafomem read surface** — it has no subject there. The
built path is ready for whichever first case the Foundation chooses under option (1), or is superseded
by a geiant-side surface under (2)/(3).
