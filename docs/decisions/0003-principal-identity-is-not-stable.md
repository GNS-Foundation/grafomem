---
status: proposed
decision_date: "—"
record_date: 2026-08-31
provenance: surfaced during the geiant .env.agent exposure response (2026-08-31)
scope: geiant delegation certificates; agent/principal identity (reference implementation)
---

# 0003 — Principal identity is not stable

- **Status:** **Proposed** (open — describes current behaviour, does not bless it)
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

## Decision (open — two options, not resolved here)

**(a) Principals become custody-managed.** `setup-agent.ts` refuses to run without an explicit
`PRINCIPAL_SK`, or an explicit `--new-principal` flag, and emits the secret once for custody
handoff. Preserves the delegation model's intent: a principal is a durable root that vouches for
agents over time.

**(b) Principals are formally declared ephemeral and per-generation.** Nothing is permitted to
anchor trust to `principal_pk`, and the standard says so. Honest about current behaviour, but it
concedes that the principal layer carries no continuity meaning.

**This record does not pick one.** It does argue that the status quo is the worst available option:
it *looks* like (a) — a named principal, a signature, a registry column — and *behaves* like (b).

Either path additionally requires a **trusted-principal allowlist** at verification time; without
one, neither (a) nor (b) yields a meaningful trust root.

## Open questions

1. Does anything today anchor trust to `principal_pk`? (Registry rows exist; no verifier consults them.)
2. What is the intended relationship to the GCRUMBS identity key that the script's own comment
   recommends for production use?
3. Should the standard require a trusted-principal set as a conformance condition, rather than
   leaving it to implementations?
4. Relationship to [0004](0004-no-identity-continuity-across-rotation.md): principal instability is
   *why* rotation continuity cannot currently be expressed. If principals are ephemeral (b), a
   continuity relation is the only remaining way to link identities across a rotation.
