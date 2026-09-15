# 0013 — Erasure-certificate issuance requires a prior ledger entry (issuance obligation, not a validity property)

**Status:** proposed 2026-09-15 · **Relates to:** [0003](0003-principal-identity-is-not-stable.md),
[0005](0005-custody-managed-principals.md) (certificate validity = signature); grafomem-internal I0b.

## Question

The `erasure_ledger` is the record `restore_scrub` replays to keep legally-erased data erased after a
backup restore (restore-independence). Today `issue_certificate` **persists the certificate before the
ledger row, and skips the ledger when the erasure is unsigned or the ledger is unconfigured**
(`erasure_proof.py:369` cert INSERT at Step 5, then `:390` ledger write gated on
`self._erasure_ledger and signature is not None`). Should the ledger entry be **a precondition of a valid
erasure certificate**?

## Decision

**No — not of a *valid* certificate; yes — of a *conformant issuance*.**

Certificate **validity** is, per 0003/0005, exactly *"the signature verifies against a trusted issuer
key."* A third party verifying a certificate has **no access to the operator's `erasure_ledger`**, so
"ledger present" is **not a checkable property of the certificate** and cannot be part of validity without
making every external verification impossible. The spec is **silent** here, and correctly so on the
verification side.

What the spec SHOULD state — and this record proposes — is an **issuance obligation**, enforced at
issuance and by conformance, distinct from validity:

> **An operator MUST durably record the erasure in the restore-scrub ledger BEFORE the erasure
> certificate is durable (subject erasure) and BEFORE the tenant key is destroyed (tenant destruction).
> If the ledger write is impossible — unconfigured, unreachable, or failing — the operation MUST abort
> and MUST NOT issue a certificate or destroy the key. The ledger write MUST NOT be skipped for an
> unsigned or otherwise incomplete erasure.**

### Required order

- **Subject erasure** (`issue_certificate`): scrub → **ledger row (committed)** → sign + persist
  certificate. (Today: scrub → sign → persist cert → *maybe* ledger.)
- **Tenant destruction** (`destroy_tenant_key`): **ledger row (committed)** → destroy DEK. (Today already
  ledger-first at `admin_routes.py:243→246`, but **not fail-closed** if the ledger is absent/failing.)

### Why ledger-first, and the accepted residue

The ledger and the certificate share the database but are written by **different roles** (`grafomem_ledger`
vs the app role) — deliberately, so the app cannot rewrite the ledger — so there is **no single ACID
transaction** across them. Ledger-first means a certificate/destroy failure *after* the ledger commit can
leave an **orphan ledger row** (an erasure recorded whose certificate/destroy didn't complete). This is the
safe direction: the ledger is append-only, restore-scrub is idempotent, over-coverage never resurrects
data. The eliminated direction — certificate/destroy with **no** ledger row — is the one that breaks
restore-independence.

## Consequences

- A certificate whose ledger row is absent is still **cryptographically valid** but was **issued in
  violation of this obligation**; detection is **operator-side** (a reconciliation sweep for
  `erasure_certificates` rows with no matching `erasure_ledger` row), never verifier-side.
- The whitepapers' "Erasure cascade ✅ VALIDATED (cert issued)" covered *a certificate was issued*, not
  *the ledger precondition held* — this record narrows that claim (same "declared ≠ behaviour" class as
  the priv-esc finding).
- Enforcement is grafomem-internal I0b: reorder both write sites + three fail-closed gates (empty
  governance/coverage; unsigned; unconfigured/unreachable ledger), each with a must-fail test.

## Open (for acceptance)

1. Empty governance/coverage at issuance — **refuse** (recommended) vs allow-with-a-non-conformant flag.
2. Self-host with no ledger — **required by default** (recommended), explicit `ERASURE_LEDGER_OPTIONAL=1`
   opt-out only.
3. Orphan-ledger reconciliation sweep — recommended, separate.
