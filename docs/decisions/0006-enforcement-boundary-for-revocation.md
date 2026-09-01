---
status: proposed
decision_date: "—"
record_date: 2026-09-01
provenance: raised-from-implementation (GNS-Foundation/geiant #11, #12, surfaced in the 2026-08-31 rotation)
scope: two jurisdictions — (implementation) geiant mcp-audit AUDIT_INIT + agent_registry, covering Question A and Question B1/B2; (standard) whether CGR conformance requires enforcement at every consumer, covering Open question 2. See "Scope — two jurisdictions" below.
---

# 0006 — The enforcement boundary: what revocation guarantees, and what lies outside it

- **Status:** **Proposed** — states the question and options; does not pick one.
- **Record date:** 2026-09-01

## Context

The 2026-08-31 GEIANT key rotation surfaced two findings that share one root: **the enforcement
boundary of revocation was never explicitly drawn.** In both, the current behaviour is an accident of
implementation rather than a decision.

- **[geiant#11] — missing registry row is fail-OPEN by accident.** The outermost `AUDIT_INIT`
  agent-denylist gate does `agent_registry … .eq('agent_pk', pk).single()` and destructures only
  `{ data }`, discarding the `.single()` error. Zero rows ⇒ `agentRevokedAt = null` ⇒
  `checkRevocation(null)` returns *allowed*. An agent with no registry row passes a gate designed to
  fail closed — because an error is ignored, not because anyone decided unregistered agents are
  trusted. (The `after_breadcrumb_insert` trigger `trg_update_agent_stats` has the same shape: a
  zero-row `UPDATE` silently drops the stats update instead of signalling.)

- **[geiant#12] — enforcement covers 1 of 3 services.** `#9` enforces revocation inside the
  `AuditEngine`, but **only `mcp-perception` constructs one.** `apps/router` and `mcp-agentcore` hold
  the agent identity yet declare no `@geiant/mcp-audit` dependency, so they never run the gate. A
  revoked `agent_pk` is refused by one service and permitted by two. The coverage boundary is an
  **absent package dependency**, not a decision — and a clean boot after the merge was nearly misread
  as enforcement passing when for two services it was enforcement being absent.

Common shape across all three: **a missing prerequisite (registry row, engine, dependency) produces
silent non-action rather than a signal.** The standard advertises revocation; today "revoked" binds
only where an engine happens to be wired, and admits anyone the registry happens not to list.

## Scope — two jurisdictions

This record deliberately spans two jurisdictions, and keeps them separate so the decision does not
blur an implementation choice into a standard commitment (or vice versa):

- **Implementation (geiant reference implementation).** *How `mcp-audit` handles a missing
  `agent_registry` row* (**Question A**) and *which geiant services are wired to enforce*
  (**Question B**, options B1/B2) are architecture choices for the reference implementation. They can
  change without changing the standard.
- **Standard (CGR conformance).** *Whether conformance requires enforcement at every consumer*
  (**Open question 2**) is a property of the standard itself — it constrains any implementation, not
  just geiant. It is the one part of this record that, once decided, binds conformant third-party
  verifiers.

Each question below is tagged with its jurisdiction. The split matters: an implementation may pick
B2 (enforce only at chain-write) while the *standard* still declines to bless "revoked-where-enforced"
as conformant — those are not the same decision.

## Decision

*Questions stated with options; unresolved until a `decision_date` is set.*

**Question A — principals outside the registry.** *(Jurisdiction: implementation.)* Does an agent
absent from `agent_registry` get permitted or denied?

- **A1 — permit unregistered** (lazy provisioning). Registration is a convenience; first audited op
  may precede the row. Requires: handle the `.single()` error explicitly, and *document* that an
  unlisted principal is trusted until listed-and-revoked.
- **A2 — deny unregistered** (strict allowlist). Unknown principal ⇒ fail closed. Requires: rotation
  and provisioning guarantee the row exists before the first audited op; the runbook already makes
  row-insertion a manual step, so the cost is bounded.

**Question B — enforcement coverage.** *(Jurisdiction: implementation for B1/B2 as a geiant
architecture choice; but whether conformance mandates uniform enforcement is the standard — see Open
question 2.)* Must every service that holds an agent identity enforce revocation, or is enforcement
deliberately scoped?

- **B1 — uniform.** Every identity-holding service enforces (wire `mcp-audit` into `router` and
  `agentcore`). Revocation means the same thing everywhere.
- **B2 — scoped to chain-writers.** Only services that write to the chain enforce; read-only
  proof/display services are explicitly exempt, documented as such, and constrained so they cannot
  take authority-bearing actions on a revoked identity.

**Cross-cutting requirement, independent of the answers.** Whichever branch each question takes, the
silent-non-action paths must become **signals**: the ignored `.single()` error, the zero-row
`UPDATE`, and the absent-dependency coverage gap should each fail loudly or be asserted in the
conformance suite, so the enforcement boundary cannot silently move again.

## Consequences

- **Strict / uniform (A2 + B1):** the strongest guarantee, and a breaking change — new call sites,
  a conformance version bump, and governance for who is in the trusted set (directly adjacent to
  [0005](0005-custody-managed-principals.md)'s trusted-principal set). Most work.
- **Permissive / scoped (A1 + B2):** the smaller change, but the standard must then state plainly
  that **"revoked" binds only where enforced**, and that a relying party may not infer global refusal
  from a revocation. The trust model becomes "revoked-at-write-time," which must be advertised
  honestly.
  - **This is a positioning constraint, not just a technical footnote.** The product's pitch is that
    a supervisor can reconstruct what an agent was *authorised to do*. Under "revoked binds only where
    enforced," a revocation is no longer global, and the reconstructable claim weakens to *"authorised
    somewhere, refused elsewhere"* — materially weaker for a compliance/oversight buyer. It reads like
    an implementation detail and is actually a limit on what the product can honestly claim. Decide it
    with that in view, not as a wiring choice.
- **Either way**, the accidental behaviours in #11/#12 get replaced by stated ones, and the
  fail-silent paths become fail-loud.

## Adjacency to the v4 work (P1)

The P0.4 decision adopts a generic relation edge in `cgr.attestation.v4`, and its revocation story is
a **revocation edge record plus a `geiant#9` enforcement column** (see
[0004](0004-no-identity-continuity-across-rotation.md) and the P1 roadmap). That work defines how
revocation is *expressed*; this record defines where it is *enforced*. Expression without a defined
enforcement surface is the same class of gap 0004 identified — a signed edge that only some consumers
honour is worth what a `continues` edge signed by an ephemeral principal is worth: little.

These are **separable questions that happen to need settling in the same window**, and that is the
argument for a standalone record rather than folding this into P1: if the enforcement boundary is
decided as an implementation detail of a schema change, it inherits the schema's framing. Kept
separate, P1 has a record to point at. So — settle them **together in P1**, but as two decisions: if
v4 introduces a signed revocation edge, Question B decides who is obligated to honour it, and Question
A decides the default for a principal the edge has never named.

## Open questions

1. Is "enforced only at chain-write" (B2) an acceptable posture for a standard that advertises
   revocation? What is a relying party entitled to assume "revoked" means?
2. *(Standard.)* Should **conformance** require enforcement at every consumer, making non-enforcing
   SDK usage non-conformant — or is enforcement an integrator responsibility the standard only
   documents?
3. One record or several? This, [0005](0005-custody-managed-principals.md)'s trusted-principal set,
   and the v4 revocation edge are three faces of one trust-boundary question. Decide whether 0006
   subsumes them or cross-references.
