# 0016 — Erasure certificates are factual: refuse present facts, bind a server-observed digest, derive non-claims

**Status:** proposed 2026-11-03 · **Relates to:** [0013](0013-erasure-certificate-issuance-requires-ledger-entry.md)
(issuance obligation), [0014](0014-erasure-coverage-status-vocabulary.md) (coverage vocabulary, vacuity gate),
[0015](0015-checkpointer-crypto-erasure-stays-adapter-local.md) (the adapter that already meets these tests).
Trigger: community review of the LangGraph checkpointer (forum.langchain.com #4137) asked whether an erasure
receipt commits to *what* was erased; auditing the server against the same question found the gaps below.

## Question

A signed erasure certificate is presented to auditors and data subjects as evidence. 0013 made sure it is
never issued without a ledger row; 0014 made sure its `coverage` is probed, not defaulted. Three ways
remained for a certificate to be *signed, ledgered, probed — and still not factual*:

1. **A certificate for a non-erasure.** `present` is a *verified* status (0014), so a fact that survived
   deletion passed the vacuity gate and still received a signed "GDPR Article 17" certificate, with
   `coverage.primary = "present"` recorded inside it. Honest in its fields, misleading as an artifact.
2. **No binding to the erased content.** `fact_content_hash` was `null` on the one path that could observe
   the content (`POST /v1/stores/{id}/delete` never read the record before deleting it) and
   *caller-asserted* on `POST /v1/erasure/issue`. The certificate committed to the fact that an erase
   happened, not to what was erased — the exact gap the checkpointer closed in 1.1.0.
3. **Asserted non-claims.** `governance_record.non_claims.coverage_gaps` was the literal `[]` with the
   comment `# Verified offline`; the MCP `verify_erasure` tool fabricated a `sealed_probe` whenever a
   certificate existed; three regulatory-report findings were the constant `"COMPLIANT"` and
   `erasure_proof_active` was `... or True`. Values asserted where they should have been measured —
   the same class as the `policy_evaluation_hash` finding.

Plus one key-handling hole: `POST /v1/erasure/issue` accepted a caller-supplied `signing_key`, so a
"platform-signed" certificate could be signed under a key of the caller's choosing.

## Decision

**A certificate attests an erasure that happened, bound to what was erased, carrying only measured
non-claims.** Concretely:

- **Gate 1b (refuse present).** After the 0014 probe, `coverage["primary"] == "present"` raises
  `FactStillPresent`; the route maps it to **409** `{status: not_issued, reason: fact_still_present}`.
  Ordered before the ledger write, like gate 1, so a refusal leaves no orphan row. "Record what was
  observed" (0014) still holds — the observation belongs in the probe's response and the decision
  trail, not in an erasure certificate. *Rejected alternative:* issue and mark `status: NOT_ERASED` —
  defensible under 0014's letter, but it makes every downstream reader responsible for noticing a
  field; refusing fails closed.
- **Server-observed digest.** The delete endpoint reads the record **before** deleting (new optional
  backend method `get(ref) -> Memory | None`, the sibling of `exists()` under the same POINT_LOOKUP
  gate; implemented for SQLite, Postgres, HNSW) and passes its content to `issue_certificate`, so
  `fact_content_hash` is a BLAKE2b-128 digest the server computed itself. The hash is of the
  plaintext (decrypted as `retrieve()`/`audit()` return it), so encryption at rest does not change the
  binding. A backend without `get()` yields a certificate with no hash — never a fabricated one, and
  the read failure is logged, not swallowed. `/v1/erasure/issue` keeps accepting `fact_content` for
  already-deleted facts, documented as **caller-asserted**; the server cannot check it.
- **Derived non-claims.** `coverage_gaps` = every recorded subsystem whose status is not
  `present`/`absent`. MCP `verify_erasure` reports `sealed_probe: null` until a probe is actually
  persisted at issuance; `ERASED_VERIFIED` is reachable only through a real `reverify` + `probe_query`.
  Regulatory findings are computed: Art. 17 from the erasure service's own stats (no service → PARTIAL;
  none issued → INSUFFICIENT_DATA; unsigned → PARTIAL), Art. 25 from whether signing and certificates
  are configured, DORA Art. 9 from which policy types are **active** for the tenant — each section now
  carries an `evidence_basis` separating `observed` from `declared` items, and a
  `policy_enforcement_scope` note stating that governance policies are evaluated via
  `/v1/governance/evaluate` and the orchestrator, not inline on memory operations. Tenant isolation is
  described as *logical per-tenant residency*, not partitioning "at database level".
- **Platform key only.** `signing_key` and the accepted-but-ignored `memory_deleted` are removed from
  `IssueErasureRequest`. No shipped client sent `signing_key`; older SDKs still sending
  `memory_deleted` are ignored, not rejected.

**No certificate schema change.** `fact_content_hash` and `governance_record` were already signed,
nullable/stored fields; `verify_certificate` reconstructs the digest from the stored columns, so
certificates issued before this record verify exactly as before, and deployed verifiers are untouched.

## Consequences

- `/v1/erasure/issue` is now a 409 for a fact that is still present. Callers that relied on
  certifying undeleted facts were relying on a non-erasure certificate; there is no compatible path.
- The report JSON for GDPR Art. 17/25 and DORA Art. 9 gains `evidence_basis` and new boolean keys; the
  existing keys keep their names but now carry computed values (`rate_limiting: false` is a possible,
  honest output). Reports can now fail.
- Left for a follow-up decision (item 2 of the audit): hash-chaining `erasure_ledger` (additive
  `seq`/`prev_hash`), and wiring an `ErasureProofService` into the MCP process so `delete_memory` can
  issue real certificates — its current dead `db_url` branch is deliberately untouched here. Per-subject
  crypto-erasure in the Cloud tier is a separate, product-level decision (item 3).
- Non-vacuity: every guard above has a test that fails with the guard reverted
  (`tests/test_erasure_honesty.py`, `tests/test_mcp_governance.py`); the end-to-end digest test failed
  on its own before `get()` existed, which is how the missing point-read was found.
