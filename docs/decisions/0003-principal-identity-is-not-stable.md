---
status: accepted
decision_date: 2026-08-31
record_date: 2026-08-31
provenance: surfaced during the geiant .env.agent exposure response (2026-08-31)
scope: geiant delegation certificates; agent/principal identity (reference implementation)
---

# 0003 — Principal identity is not stable

- **Status:** **Accepted** 2026-08-31 — resolved to **(b)**, as a *description of what the
  system does*, not an endorsement of it. The target design is
  [0005](0005-custody-managed-principals.md).
- **Record date:** 2026-08-31

## Context

`packages/mcp-audit/scripts/setup-agent.ts` in the GEIANT reference implementation generates the
principal keypair **in memory**, uses the secret to sign the delegation certificate, writes only
`principal_pk` to `.env.agent`, and then discards the secret. When `PRINCIPAL_SK` is absent from the
environment the script **silently mints a new principal**, logging only
`🔑 No PRINCIPAL_SK provided — generating NEW principal keypair` before proceeding identically to
the reuse path.

**This has already happened, unnoticed, for roughly five months.** The principal registered in
`delegation_certificates` is `262507c6…92bfa`. The principal in the working `.env.agent` — and on
two production Railway services — is `39545553…cc94a`. Two unrelated root identities exist for a
single agent (`c14094ea…bc04`), and the divergence surfaced only because an unrelated credential
exposure forced an audit of the registry.

Nothing detected it because nothing *could*: a certificate is verified against the `principal_pk`
carried **inside the certificate itself** (`verifyDelegationCert` in `chain.ts`), and there is no
trusted-principal allowlist anywhere in the implementation. A self-consistent certificate from any
principal verifies. The second principal was therefore not "invalid" — it was simply unregistered,
and would have been registered on first use by the insert-on-first-sight path.

## Consequences

- A principal identity **cannot be reused after its first run**. The secret is unrecoverable by
  construction, so re-signing a certificate for an existing principal is impossible without custody
  material the tooling never emits.
- Any trust chain anchored to a principal **breaks silently** on re-run. No error, no warning, no
  drift check.
- `delegation_certificates.principal_pk` is **not a stable anchor** and must not be treated as one
  by downstream verifiers.
- Because certificates are self-vouching, principal rotation is indistinguishable from principal
  *substitution*. Revoking a certificate does not constrain the agent: the holder of an agent secret
  can mint a fresh principal, self-sign a new certificate for the same `agent_pk` at any scope, and
  pass verification. Revocation must therefore bind to the **agent**, not the certificate.

## Decision — resolved to (b), 2026-08-31

**Under the current implementation, principals are formally ephemeral and per-generation.
Nothing may anchor trust to `principal_pk` as it stands.**

This is a statement of fact about the system as built, adopted so that downstream work stops
assuming a property the code does not provide. **It is not an endorsement.** The principal layer as
implemented carries no continuity meaning, and this record does not argue it should stay that way.

What follows from accepting (b):

- `delegation_certificates.principal_pk` is **descriptive metadata, not a trust anchor.** Verifiers
  MUST NOT treat two certificates sharing a `principal_pk` as related, nor treat a change of
  `principal_pk` as meaningful. It records which ephemeral key signed a given certificate, nothing more.
- A certificate is **self-vouching**: `verifyDelegationCert()` checks the signature against the
  `principal_pk` inside the certificate, and no trusted-principal allowlist exists. Under (b) this is
  not a bug to be patched in isolation — it is the honest consequence of ephemeral principals.
  Certificate validity therefore says only "this was signed by whoever claims to have signed it."
- **Revocation cannot bind to a certificate.** It must bind to the `agent_pk`. Implemented in
  GNS-Foundation/geiant#9 (`agent_registry.revoked_at`) after the certificate-level approach was
  demonstrated bypassable in production: two valid certificates existed for one compromised
  `agent_pk`, one revoked and one not.
- Rotation produces an identity with **no expressible link** to its predecessor — see
  [0004](0004-no-identity-continuity-across-rotation.md).

### Why (a) was not adopted now

**(a) custody-managed principals is the better design and remains the target** — recorded separately
as [0005](0005-custody-managed-principals.md). It was not adopted here because it is **not yet
survivable**: a stable, custody-held principal is only worth having if an identity can carry its
history across a rotation, and no `continues` relation exists to express that
([0004](0004-no-identity-continuity-across-rotation.md)). Adopting (a) today would make rotation
*more* painful — a custody ceremony on top of an already-orphaned chain — which in practice means it
gets routed around. 0005 is therefore explicitly blocked on 0004.

Accepting (b) is the accurate description of today. It is not the destination.

## Open questions

Closed by this decision:

- ~~Does anything today anchor trust to `principal_pk`?~~ **Nothing may, as of 2026-08-31.**
  Registry rows exist and no verifier consults them; under (b) none should.

Carried forward to [0005](0005-custody-managed-principals.md):

1. What is the intended relationship to the GCRUMBS identity key that `setup-agent.ts`'s own comment
   recommends for production use? Under (b) it is simply another ephemeral signer; under (a) it is
   the natural custody root.
2. Should the standard require a trusted-principal set as a conformance condition, rather than
   leaving it to implementations? Only meaningful once principals are stable.

Dependency chain — read in this order:

> **0003** (accepted, describes today: ephemeral principals)
> → **[0005](0005-custody-managed-principals.md)** (proposed target: custody-managed principals)
> → **[0004](0004-no-identity-continuity-across-rotation.md)** (blocker: no `continues` relation)

Principal instability is *why* rotation continuity cannot currently be expressed. Under (b) a
continuity relation is the **only** remaining way to link identities across a rotation — and 0005
cannot be adopted until that relation exists.
