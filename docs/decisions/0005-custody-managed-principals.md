---
status: proposed
decision_date: "—"
record_date: 2026-08-31
provenance: split from 0003 when that record was accepted to option (b) (2026-08-31)
scope: cgr.attestation.v3; geiant agent/principal identity (reference implementation)
---

# 0005 — Custody-managed principals (target design)

- **Status:** **Proposed** — the **target** design. (Blocker cleared at P0.4; see below.)
- **Blocked on:** [0004](0004-no-identity-continuity-across-rotation.md) — **CLEARED 2026-08-31
  (P0.4).** 0004 is accepted, adopting a **Foundation-signed** `continues` relation edge in
  `cgr.attestation.v4`. Because the edge is issuer-signed, this record no longer waits on stable
  agent principals to *express* continuity. It remains **Proposed**, not accepted: custody-managed
  principals are a distinct design with their own adoption cost (trusted-principal set, a breaking
  change for verifiers, trusted-set governance) — downstream of the `v4` mechanism, not automatic
  with it.
- **Supersedes in intent (not in force):** the option (a) branch of
  [0003](0003-principal-identity-is-not-stable.md), which was accepted to (b) as a description of
  current behaviour.
- **Record date:** 2026-08-31

## Context

[0003](0003-principal-identity-is-not-stable.md) was accepted to option (b): principals are formally
ephemeral and per-generation, and nothing may anchor trust to `principal_pk`. That is an accurate
description of the implementation, and deliberately not an endorsement. This record holds the
design we actually want, so that (b) is not mistaken for a destination.

**Target.** A principal is a **durable root held in custody** that vouches for agents over time:

- `setup-agent.ts` refuses to run without an explicit `PRINCIPAL_SK`, or an explicit
  `--new-principal` flag; it never silently mints a principal.
- When a principal *is* minted, the secret is emitted **once**, for custody handoff, following the
  ceremony in the Foundation key-custody runbook.
- Verification consults a **trusted-principal set**. A certificate is valid only if its
  `principal_pk` is in that set — closing the self-vouching hole, whereby any keypair can sign a
  certificate for any `agent_pk` it holds and pass `verifyDelegationCert()`.
- `delegation_certificates.principal_pk` becomes a genuine trust anchor: two certificates sharing a
  principal are meaningfully related, and revocation can bind at the principal level as well as the
  agent level.

## Why this is blocked on 0004

A stable principal is only worth having if an identity can **survive** a rotation. It cannot today.

Under (b), rotating an agent produces a new, unrelated chain: the predecessor's breadcrumbs, epochs,
trust score, and tier are orphaned, because no relation edge can express "this continues that"
([0004](0004-no-identity-continuity-across-rotation.md)). Adopting custody-managed principals on top
of that would make rotation **strictly more expensive** — an offline custody ceremony *plus* an
orphaned chain — while removing none of the existing cost.

The predictable result is that the ceremony gets routed around. That is not speculation: the
2026-08-31 GEIANT exposure resolved to *revoke but do not rotate*, precisely because rotation was
punitive and revocation was cheap. A control that is expensive to exercise is a control that does
not get exercised.

There is also a direct dependency in the mechanism. A rotation-continuity record must be signed by
some authority that outlives the rotated key. Signing with the **outgoing agent key** is wrong when
that key is compromised — the usual reason to rotate. The natural signer is the **principal**, which
requires the principal to be stable — which is this record. So 0004 and 0005 are mutually
reinforcing, and 0004 must land first: a `continues` edge signed by an ephemeral principal is worth
little, but a stable principal without a `continues` edge is worth less than it costs.

**Sequencing:** 0004 (relation mechanism) → 0005 (custody-managed principals) → deprecate 0003's (b).

## Consequences if adopted (once unblocked)

- Rotation becomes a custody operation with a real ceremony — acceptable **only** once the chain
  survives it.
- `verifyDelegationCert()` gains a trusted-principal parameter; every call site and the conformance
  suite change. This is a breaking change for verifiers and needs a conformance version.
- The trusted-principal set itself needs governance: who may add to it, how it is distributed to
  verifiers, and how a principal is retired. That is plausibly its own record.
- Existing certificates signed by ephemeral principals become unverifiable under the new rule unless
  grandfathered — which is itself a `supersedes`/`continues` problem, i.e. 0004 again.

## Consequences of *not* adopting

The status quo persists: certificates vouch for themselves, revocation can only ever bind to
`agent_pk`, and compromise of any agent secret allows the holder to self-sign arbitrary scope for
that agent. GNS-Foundation/geiant#9 contains that specific failure mode at the agent level, but it
does not close the underlying hole.

## Open questions

1. What is the custody root — the GCRUMBS identity key that `setup-agent.ts`'s comment already
   recommends, the Foundation signing seed, or a distinct per-facet principal? (Carried from 0003.)
2. Should the trusted-principal set be a conformance condition of the standard, or an
   implementation concern? (Carried from 0003.)
3. Distribution: how does a verifier learn the trusted set, and how is a retired principal
   propagated?
4. Grandfathering: do certificates signed by ephemeral principals remain verifiable, and under what
   marker?
