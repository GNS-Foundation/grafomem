---
status: proposed
record_date: 2026-09-04
corrected_date: 2026-09-05
provenance: raised-from-implementation — surfaced during the read-only pre-flight of the §5.3 continues-edge ceremony for the c14094ea → d3caa6f1 rotation (2026-09-04). CORRECTED 2026-09-05 after a live-wiring investigation found a THIRD actor the original sweep missed (see "Correction").
scope: (standard) the CGR continues edge (§1, §5.3) and 0004 rotation-continuity; (implementation) the shared GNS identity Supabase, gns-backend, geiant, and grafomem — the systems that jointly touch agent identity
---

# 0008 — identity continuity has no shared data path between the systems that need it

- **Status:** **Proposed** 2026-09-04, **corrected 2026-09-05** — this states the problem; it does not
  resolve it. It records a structural gap found when the `continues` edge was ready to issue and had
  no valid first case.
- **Record date:** 2026-09-04
- **Relates to:** [[0004]] (no identity continuity across rotation — the motivation), [[0005]]
  (custody-managed principals), [[0006]] (enforcement boundary; label non-strippability), [[0007]]
  (no reverse edge index), and `cgr-attestation-v4-spec.md` §1 / §5.3 (the continues edge + ceremony).

## Correction (2026-09-05) — read this first

The original 0008 (below, revised) named **two** actors — geiant and grafomem — and called their data
"disjoint." **That framing was incomplete.** The sweep behind it covered only the grafomem and geiant
repos; it missed a **third actor: the shared GNS identity Supabase database** (project
`kaqwkxfaclyqjlfhxrmt`), which holds the agent identity registry and breadcrumb chains — including
both `c14094ea` and `d3caa6f1`. A follow-up **live-wiring investigation (2026-09-05)** established the
real topology, corrected here. The record's **conclusion is unchanged** — there is still no shared
*lineage* data path, and grafomem's CGR surface still has no path to the identity data — but the
**options change**, so the correction is recorded visibly rather than silently.

## Disambiguation — "GCRUMBS"/"breadcrumbs" is four things

The name misleads; separate the senses or this record will be misread:

1. **GCRUMBS the product** — a consumer gamification app (Foursquare-style) in `gns-mobile`/
   `gns_browser`. **Explicitly not the identity layer** ("Not in gns-backend", trust-architecture
   spec). Irrelevant here.
2. **The breadcrumb chain (a primitive)** — an append-only, Ed25519-signed, per-agent hash chain +
   Merkle epochs. It exists as **three separate instances**: gns-backend's schema (`agent_breadcrumbs`
   in the shared Supabase), grafomem's own local chain (`gcrumbs.py`, grafomem's DB), and geiant's
   audit chain (`gns_verify_chain`/`gns_roll_epoch`). Same design, three stores.
3. **"A GCRUMBS identity key"** (geiant `setup-agent.ts`) — shorthand for a **stable principal
   identity** to use instead of a generated ephemeral one (the [[0003]]/[[0005]] problem). Not wired.

When this record says "the identity layer," it means the **shared identity Supabase database** (sense
adjacent to 2), never the product (1).

## Context — the corrected topology (three actors, not two)

`subject_key` in the CGR attestation **is** the agent's geiant Ed25519 pubkey, captured at decision
time (Ticket #5). So all three actors share **one `agent_pk` identity namespace**. But they do not
share a data path:

- **The shared GNS identity Supabase** (`kaqwkxfaclyqjlfhxrmt`) — `agent_registry`,
  `agent_breadcrumbs`, `agent_epochs`, `delegation_certificates`, on the `agent_pk` namespace. **Holds
  both `c14094ea` and `d3caa6f1`.** **geiant writes to it directly** (mcp-audit `middleware.ts` →
  `.from('agent_breadcrumbs')` / `.from('agent_registry')`, `SUPABASE_URL=…kaqwkxfaclyqjlfhxrmt…`) and
  is **live** (mcp-perception is deployed). This DB is the real identity store.
- **gns-backend** — the API server that **owns that schema in code** (its migrations created those
  tables). **It is NOT deployed:** its Railway project has **zero services**, and geiant bypasses it
  by writing Supabase directly. So gns-backend is a dead API in front of a live DB. **Any option that
  names gns-backend as a host must state that it is not currently running.**
- **grafomem** — a **separate system**: its own database, its own local breadcrumb chain
  (`gcrumbs.py`), **no connection to the shared Supabase**. Its CGR substrate is built from Ticket-#1
  captured governed decisions joined to resolved outcomes — a different dataset from the identity
  registry. It shares the `agent_pk` namespace **by convention only**.

**Verified live (2026-09-04/05), read-only:** the grafomem read surface serves only `*@ulissy` and
`*@virtualbank` (Meridian sim) subjects — **no geiant agent has CGR substrate**; `d3caa6f1` is not a
subject there. And grafomem holds no wiring to `kaqwkxfaclyqjlfhxrmt`.

## Consequence

The `continues` edge is specified in the CGR attestation (§1) and injected at **grafomem's mint** — but
[[0004]]'s **rotation-punitiveness** is about the **identity registry's** orphaned score and breadcrumb
chain, which live in the shared Supabase (where geiant writes), not in grafomem's CGR substrate. A
geiant rotation can express continuity on the grafomem read surface **only if that agent is also a
scored grafomem CGR subject** — and `c14094ea → d3caa6f1` is not. **So the case that motivated the
design cannot carry the edge it motivated**, and grafomem has no path to the identity data where the
motivation actually lives.

## What survives and sharpens

- **Lineage exists in none of the three** — including the shared identity database, whose job it most
  obviously is. `delegation_certificates` records **principal→agent authority**, which is **not key
  succession** (a new key does not "continue" an old one; a principal grants scope to a separate agent
  key). There is **no** rotation/predecessor/successor/continues record type anywhere. **A `continues`
  edge is net-new everywhere.**
- **grafomem's CGR read surface has no path to the shared identity database at all.** That part of the
  original finding is unchanged and is the sharpest version of it: even where the identity data lives
  (the shared Supabase, live via geiant), grafomem is not attached to it.

## What was built is correct — it simply has no first case

Unchanged from the original. The edge store (`cgr.continues.v1`), the mint injection
(`build_read_envelope` → signed-body `relates_to`), the one-shot ceremony
(`scripts/cgr_continues_ceremony.py`, four §5.3 preconditions), and the conformance vectors **CL1/CL2**
all work correctly **for grafomem CGR subjects**. They are landed and tested; they lack a valid first
subject on this surface, not correctness.

## Options (restated for the corrected topology — still not choosing)

1. **A different subject** — issue the first `continues` edge for a **grafomem CGR subject that
   actually rotates** (an Ulissy/Meridian agent with captured decisions + resolved outcomes). Exercises
   the built machinery on the surface it was built for; does not, by itself, serve [[0004]]'s
   geiant-rotation case.
2. **Lineage in the shared identity DATABASE** — a rotation/lineage table in `kaqwkxfaclyqjlfhxrmt`
   **beside `delegation_certificates`**, written through **geiant's existing direct-Supabase path**.
   **Achievable today** (the live writer and the DB both exist), and it puts key succession next to the
   identity data it's about.
3. **Lineage via gns-backend AS A SERVICE** — **hypothetical.** Requires first **deploying** gns-backend
   (its Railway project has no services) and then wiring callers to its HTTP API — i.e. building the
   connective tissue this record says is missing. Not a today-option.
4. **Cross-system surfacing is a separate cost regardless.** Even if lineage lands in the shared
   identity DB (option 2), it would **not surface on grafomem** without **new cross-system wiring** —
   grafomem does not read that database. So "where lineage is written" and "where lineage is shown"
   are two decisions, not one.

## Interaction with the other open questions

Whichever surface issues/hosts lineage **inherits the still-open questions**, so this should not be
decided in isolation:

- **Label non-strippability ([[0006]], open):** an emitted lineage/enforcement label must be
  non-strippable on whatever surface carries it.
- **The reverse index ([[0007]], open):** `continues`-only lineage does **not** need it (Lineage-
  Degrades), but any surface that also carries `revokes`/`supersedes` does.
- **What `continues` transfers (§5.3, SPLIT — navigable lineage decided, CGR pooling deferred):** if
  lineage lives in the identity DB, "what transfers" must be answered for the identity registry's
  score/chain, not only grafomem's CGR score — a different score in a different system.

## Recommendation on status

Left **open** for a Foundation decision. Immediate engineering reality, recorded: **do not run the
`c14094ea → d3caa6f1` ceremony against the grafomem read surface** — it has no subject there, and the
identity data the case is about is in a database grafomem is not connected to. The built path is ready
for whichever first case is chosen under option (1); options (2)/(3)/(4) are the identity-layer
directions, with (3) gated on deploying gns-backend at all.
