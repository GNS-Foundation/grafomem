# Decision records — GNS-Foundation / GRAFOMEM

Decision records for the **standard**: the CGR attestation schema (`cgr.attestation.v3` and
successors), the verification recipe, the conformance suite, and the governance choices that
shape them — **and for the reference implementations that exercise the standard**, where those
implementations reveal something about the standard's expressive limits. This is the
Foundation's record of *why the standard is the way it is*.

Scope note: records here concern the **standard and its reference implementations**. A record
belongs here when the finding is about what the schema can or cannot express — even when it
surfaced in an implementation (e.g. GEIANT delegation certificates in
[0003](0003-principal-identity-is-not-stable.md) and
[0004](0004-no-identity-continuity-across-rotation.md)). **Commercial/product decisions still
live in their own repos**; where the two touch, they cross-reference.

## Records

| # | Title | Status |
|---|---|---|
| [0001](0001-cgr-grounding-dimension-additive-vs-schema-bump.md) | Grounding dimension: true-additive vs schema-bump | **accepted** 2026-08-31 (P0.4 → v4 bump) |
| [0002](0002-cgr-governance-domain-and-backfill.md) | Governance domain + backfill expression | **accepted** 2026-08-31 (P0.4 → v4 bump) |
| [0003](0003-principal-identity-is-not-stable.md) | Principal identity is not stable | **accepted** 2026-08-31 |
| [0004](0004-no-identity-continuity-across-rotation.md) | No identity-continuity across rotation | **accepted** 2026-08-31 (P0.4 → generic relation edge, v4 bump) |
| [0005](0005-custody-managed-principals.md) | Custody-managed principals (target design) | proposed — unblocked (0004 resolved) |
| [0006](0006-enforcement-boundary-for-revocation.md) | Enforcement boundary: what revocation guarantees, and what lies outside it | **accepted** 2026-09-02 (A2 deny-unregistered; B2 rejected; enforce-or-label) — label-non-strippability open |
| [0007](0007-geiant-core-has-no-reverse-edge-index.md) | No reverse edge index (ecosystem-wide): enforcing-mode v4 not implementable against any store — @geiant/core or the grafomem read surface | proposed 2026-09-01 (finding + absent-entirely; extended ecosystem-wide 2026-09-01; index design & issuance gate open) |
| [0008](0008-identity-continuity-has-no-shared-data-path.md) | Identity continuity has no shared data path: shared agent_pk namespace across THREE actors (shared GNS identity Supabase — live via geiant; gns-backend write path bypassed though its read API is live; separate grafomem), but lineage recorded in none and no path to grafomem CGR | proposed 2026-09-04, **corrected 2026-09-05** (third actor), **updated 2026-09-06** (deployment precision — resolve API live; two-handle-namespace collision; shared framing with 0009) |
| [0010](0010-cosign-predicate-unresolved-must-reject.md) | `cgr.cosign.v1` predicate-unresolved must REJECT, not pass-with-a-warning — the §5.1 required-ness predicate fails OPEN as written (absent/non-scalar/null `required_when.field` ⇒ requirement optional ⇒ high-risk record passes unsigned); found writing the corpus before any implementation; three-way divergence (spec text "optional" vs commissioning instruction "fail closed" vs §8 preamble "fail closed"); amendment = unresolved ⇒ reject with `predicate_unresolved` as reason (§8.3a brought into line with §8's preamble, its sole exception removed); + §5 note (free-mode refs live in signed `content_body`, never `evidence_ref`) | **accepted 2026-09-08** (reject-on-unresolved CHOSEN over treat-as-REQUIRED-then-reject-if-unsigned; cost = malformed predicate DoSes that profile's records until fixed — visible, one-directional, bounded) |
| [0011](0011-cosign-issuer-trust-and-uniform-unresolved.md) | `cgr.cosign.v1` MUST pin the issuer key, and unresolvable is uniform across operators — two gaps of the same shape (spec assumed, never stated), found by two independently-written verifiers against one corpus. **Gap 1 (larger):** §8 never pins `issuer_key_id`, so both verifiers check the system signature against the record's SELF-DECLARED issuer key ⇒ valid under ANY consistent issuer key ⇒ approver binding holds but issuer binding does not; surfaced as an *agreement that was luck, not specification*. Amendment = §8 MUST check `issuer_key_id` against a caller-supplied trusted set, REQUIRED input, no default, no trust-everything path (attestation-v3 precedent, ticket-05). **Gap 2:** 0010 pinned field-resolution + ordering-operand type but not the `in`-operand shape ⇒ non-array `in` reads false (JS) vs unresolved-reject (Python); surfaced as a *divergence*. Amendment = unresolvable is uniform across all operators (general rule, not an enumeration) ⇒ any unevaluable predicate rejects `predicate_unresolved` | **accepted 2026-09-08** (gap 1 touches JS+Python alike — a step both missed; gap 2 aligns JS to Python; corpus adds `0x33` untrusted-signer vector + two well-formed `in` vectors, amendment lands first) |
| [0009](0009-standard-expresses-one-actor-approval-needs-two.md) | The standard expresses one actor; a defensible approval needs two — v4 models agent + issuer but has no approver principal and one signature slot (the issuer's, not the decider's); found scoping B3's AML disposition record, confirmed same-day in TrueForge and v4; general (AMLR Art. 11 accountability + Recital 38, Art. 18 non-transfer, AI Act Art. 14 oversight), belongs upstream per ADR-0008 | **accepted 2026-09-06** (resolved by a general two-party co-signature envelope `cgr.cosign.v1`, record-agnostic, with `cgr.disposition.v1` as its first profile — NOT a v4 extension; nested approver+system signatures, `grafomem.hitl.approval.v1` domain tag, tamper-evident+conformance-enforced not tamper-proof; **gap 3a verified-named-person identity NOT resolved** — separate assurance dependency; cost accepted = a third conformance surface). Prior history: gap 3 **corrected TWICE same day** (v1 "no namespace" → v2 server-only "no accountable/verified person" → v3 after reading the Dart client: person layer is **not thin, it is unassured+unwired** — `gns_browser` self-custody + `grafomem.hitl.approval.v1` signing are built; missing = KYC/IDV (PoH is proof-of-*trajectory*), mint-wiring, role-tenure; adopt mechanism not consumer identity semantics; iCloud-sync of signing key = open Q); **AMLR Art. 18 citation corrected** (Art. 18 = Outsourcing/non-transfer; the named-person-decides claim re-cited to Art. 11 + Recital 38 + AI Act Art. 14 — 3rd propagated-citation fix this week); **resolution shape sharpened by B2** (needs a general signed two-actor decision record `cgr.disposition.v1` — NOT a v4 extension: no record class fits [gap 5], one signer slot, no content-field home; evidence-digest + HMAC-pseudonym primitives generalise from provenance.py/invoice_pseudonym.py — now ACCEPTED, see above) |
| [0012](0012-product-code-resident-in-the-foundation-repo-by-exception.md) | Ulissy product code is resident in the Foundation repo **by exception** — `src/aml/cloud` (82 top-level modules) + `src/aml/static/portal` are Ulissy assets living in a **PUBLIC**, MIT-licensed © GNS Foundation repo, and shipping inside the public PyPI `grafomem` wheel; four standard→product import paths verified (`provenance.py`, `backends/interface.py` module-level; `cli.py`, `cgr/validate.py` lazy), with `src/aml/server/` named as a fifth and much larger surface held out of scope. Foundation retains spec, issuer key, conformance, `cgr-verify` and the "Grafomem" mark; the mark is **licensed** to Ulissy for "GRAFOMEM Cloud". Records the **entities' decision**; the assignment instrument is **pending execution by counsel**, and its direction is a counsel question | **accepted** 2026-09-10 (counterpart: `eu-governed-agent` ADR-0012; parent: ADR-0005) |

| [0013](0013-erasure-certificate-issuance-requires-ledger-entry.md) | Erasure-certificate issuance requires a prior ledger entry — an **issuance obligation, not a validity property**. Certificate validity is signature-only (0003/0005) and a third-party verifier has no access to the operator's `erasure_ledger`, so "ledger present" cannot be a verification-side precondition; the spec is silent there, correctly. Proposes the issuance-side rule: the operator MUST commit the restore-scrub ledger row BEFORE the certificate is durable (subject erasure) and BEFORE the DEK is destroyed (tenant destruction), fail-closed on an absent/failing/unconfigured ledger, never skipped for an unsigned erasure. Ledger-first ordering (ledger + cert are different roles → no single tx; orphan-ledger-on-failure accepted as the safe direction). Enforced by grafomem-internal I0b (reorder both write sites + three must-fail gates). **Resolved:** (1) empty governance/coverage → refuse; (2) ledger required by default, `ERASURE_LEDGER_OPTIONAL=1` (dev/test) may erase but NEVER issues a certificate ("erased, no certificate issued: ledger not configured") — no unledgered certificate in any mode; (3) orphan-ledger reconciliation sweep = separate PR after I0b. Target order: subject erasure `scrub → ledger committed → sign+persist`; tenant destruction `ledger committed → destroy` | **accepted 2026-09-15** (enforcement: I0b; narrows the whitepapers' "Erasure cascade VALIDATED" to cert-issued-not-ledger-precondition) |
| [0014](0014-erasure-coverage-status-vocabulary.md) | Erasure-certificate `coverage` status vocabulary, spec-first — the vocabulary lived only in `erasure_proof.py` (verify treats `present`/`absent` as definitive, anything else as a gap) and was **never in the spec**, while `issue_certificate` **auto-defaulted** omitted coverage to `{"primary":"absent"}` so every caller shipped a one-subsystem, never-probed claim. Defines: `present` = found still present (erasure incomplete — a failure signal), `absent` = verified not present (clean), `unverified` (legacy `unchecked` treated identically) = not checked, **no claim** = a coverage gap. Only `present`/`absent` are *verified*. A conformant certificate MUST assert coverage explicitly (never a fabricated default), and MUST carry ≥1 verified entry — the **vacuity gate (I0b gate 1)** refuses coverage with zero verified entries (`{}` or all-`unverified`, and the removed auto-default). Verifier unchanged (already treats non-`present`/`absent` as a gap); this record names `unverified`. | **proposed 2026-09-16** (enforcement: grafomem-internal I0b stage-1 coverage probe; removes the `{"primary":"absent"}` auto-default) |

**Unnumbered records.** A *design spike* scopes work without deciding anything, so it does not spend a
permanent number: [`separation-of-product-code-design-spike.md`](separation-of-product-code-design-spike.md)
(proposed 2026-09-10) prices the full extraction of the product code — with history — plus the deploy and
distribution retarget, and puts one question in front of it: does `src/aml/server/` follow the product,
stay with the standard, or split. It gets a number if it becomes a decision. It **does not gate bank contact**.

**Resolved at P0.4 (2026-08-31):** 0001, 0002 and 0004 shared one versioning question — how CGR adds
signed meaning — and it resolved to a **schema bump to `cgr.attestation.v4`**, carrying a generic
relation edge (0004) plus the grounding (0001) and governance/backfill (0002) fields. 0005 is now
**unblocked** (its own adoption is a separate decision). The `v4` schema design, migration, and
golden-fixture regeneration are P1. *(Formal acceptance lands when this PR merges.)*

## Convention

- **One numbered record per decision or open question**, `NNNN-short-slug.md`, starting at
  `0001`. Numbers are permanent; a superseded record is not deleted — a later record supersedes
  it and both link.
- **YAML front matter** on every record:

  ```yaml
  ---
  status:        proposed | accepted | superseded-by-NNNN | deprecated
  decision_date: YYYY-MM-DD   # when the decision was made; omit/"—" while still proposed
  record_date:   YYYY-MM-DD   # when this record was written
  provenance:    <how it came to be recorded — e.g. promoted-from-working-draft, backfilled, relocated-from-<repo>>
  scope:         <the schema/spec version(s) affected — e.g. cgr.attestation.v3>
  ---
  ```

  `decision_date` and `record_date` are **distinct on purpose**: a record written after the fact
  must not imply it was captured when the decision was made.
- **Body sections:** `## Context` · `## Decision` · `## Consequences` · `## Open questions`.
  A record whose `status: proposed` states the question and options under **Decision** but does
  **not** pick one — it is resolved by a later edit that flips `status` to `accepted` with a
  `decision_date`, or by a superseding record.

This format is deliberately legible alongside the ADR-style records used in product repos, but it
is the Foundation's own convention, not an import: records here are numeric (`0001`), product ADRs
are `ADR-000N`, and the two cross-link.

## Primary-source verification (load-bearing claims)

Three corrections in one week ([[0008]] two→three actors; [[0009]] gap 3 corrected twice; the AMLR
Art. 18 citation) shared **one** root cause: a finding was accepted from a **single vantage** — one
repo side, one system side, one research report — stated with **more confidence than that one source
supported**, and then **propagated** into records and toward the pitch before anyone checked it against
source. This convention exists to catch that class of error at the point of entry, not in a later
correction.

- **A _load-bearing external claim_ must be verified against a primary source before it enters a
  record or the pitch.** Load-bearing = a regulatory citation (article number **and** heading), a legal
  effect, a deployment/topology fact, a claim about another system's schema or behaviour, or anything a
  reader (or a supervisor, or a client) would rely on. The primary source is the authority itself —
  **EUR-Lex for EU law**, the official spec/standard text, the code/DDL/deploy metadata for a system
  fact — **not** a summary, a research report, a blog, or another one of our own records.
- **Cite what you verified, at the granularity you verified it.** An article citation carries its
  **heading** and the **operative sentence** (e.g. *Art. 18 "Outsourcing"*, not a paraphrase of what we
  wish it said). If a claim rests on several provisions, cite each for the part it actually establishes;
  do not let one citation carry two claims.
- **Distinguish "verified against source" from "taken from a report."** A finding still at
  report/single-source stage is **provisional** — mark it so (in `provenance`, or inline) rather than
  stating it flat. A research report is a lead to verify, not itself a primary source.
- **Two-vantage rule for cross-system / distributed findings.** A claim about a distributed system
  (client **and** server, repo **and** deploy, one repo **and** its sibling) is not "established" from
  one side. State the vantage examined and flag the unexamined one, or examine it, before asserting.
- **Corrections stay visible.** When a propagated claim is corrected, record the correction **and its
  propagation path** in the affected record (as [[0008]] and [[0009]] do) — the correction is data about
  how the error travelled, not just a fix.

This is a Foundation record-keeping discipline; product repos (ADR-style) inherit the same bar for
load-bearing claims, since a citation wrong here is wrong in the pitch too.

### Failure log

Concrete instances, kept so the convention above stays attached to what it cost. Each entry names
the claim, the vantage that produced it, and why the corroboration failed.

- **2026-09-10 — "`railway.toml` is not applied; there is no deploy gate."** *Wrong.* The claim came
  from scanning the whole `railway status --json` payload for keys matching
  `healthcheck|replica|restart` and reading the first matches as the `grafomem` service. They were
  **`Postgres`**, which has no `railway.toml` and therefore shows Railway's defaults
  (`healthcheckPath=null`, `restartPolicyMaxRetries=10`). Scoped to the right service, the manifest
  reads `configFile=/railway.toml`, `healthcheckPath=/health`, `restartPolicyMaxRetries=3` — the
  file **is** applied. The true finding is narrower and was already correct before the "correction":
  the gate is **present but vacuous**, because `/health` returns a static `ok` without touching the
  database. *Lesson: a payload containing many services is many vantages, not one — scope the query
  to the subject before reading the answer.*
- **2026-09-10 — the corroboration that was not one.** The same wrong claim was supported by "the
  API service config has no healthcheck field," offered as a second vantage. It is not: an **absent
  field does not distinguish _unset_ from _set-from-the-config-file_**, so it was consistent with
  both readings and could not discriminate between them. A second observation that cannot come out
  differently under the competing hypothesis is not corroboration. The `restartPolicyMaxRetries`
  "3 vs 10" mismatch offered alongside it was the *same* mis-scoped read counted twice. *Lesson:
  before calling something a second vantage, state what it would have shown had the claim been
  false.*
- **2026-09-10 — "the erasure daemon has no port, so it must not receive an HTTP healthcheck."**
  *Withdrawn.* Inferred from the Railway API showing no domains attached to the service. Absence of
  a public domain says nothing about whether a process binds a port:
  `src/aml/cloud/erasure_daemon.py:94-119` builds a FastAPI app, mounts Prometheus at `/metrics`,
  serves `GET /health`, and binds `PORT` (default 9091) under uvicorn — with a comment stating the
  purpose outright, *"Bind to PORT for Railway healthchecks and metrics scraping."* The healthcheck
  it has is deliberate. *Lesson: platform metadata describes what the platform was told, not what
  the process does — when the claim is about program behaviour, read the program.*
- **2026-09-11 — "multi-tenant isolation is VALIDATED."** *True for the data plane, false for the
  control plane.* The whitepapers mark tenant isolation ✅ VALIDATED (cross-tenant *store/memory*
  access → 403, two-sided P0-3). But that validation never covered the **admin/control plane**:
  `create_tenant` minted every tenant's default key with `role='admin'` + `scopes=['*']`,
  `require_scope('admin:platform')` treated `'*'` as satisfying it, and `_require_admin` only checked
  `role=='admin'` — so any tenant's default key could call platform routes for *other* tenants, and
  `list_tenants` returned **every tenant's `api_key`**. A "VALIDATED isolation" claim scoped to one
  plane read as covering both. **Exploitation is undeterminable from retained data — no audit logging
  on these routes** (the `audit_logs` table existed but the admin routes never wrote to it; proxy logs
  carry no caller tenant and have short retention). *Lesson: an isolation claim names the plane it was
  tested on; the admin plane is a separate surface and was never tested. Fixed: platform routes gated on
  operator identity (`PLATFORM_TENANT_IDS`), never on `'*'`; list/get stripped of `api_key`; admin
  routes now write `audit_logs`. See the incident record in `grafomem-internal`.*
- **2026-09-15 — the I0c backfill count, and what "14" actually was.** The I0c deliverable said the
  backfill set was "14" and annotated the extraction query "expect 14." Two things were wrong, and a
  first draft of *this very entry* added a third. **(1) Wrong denominator.** 14 is not the number of
  certificates missing a ledger row — it is a *subset*: the signed certs completed AFTER the ledger
  write shipped 2026-06-22, i.e. the ones the code itself should have written (the 2026-09-10
  erasure-ledger incident, fact #4, verified in prod). The anti-join for *all* pre-ledger certs returns
  **27** (25 signed + 2 unsigned); the 13 pre-code certs that incident called "legitimately absent" are
  still real ledger gaps. **(2) Unposed scope question.** Whether to backfill only the 14
  code-should-have-written rows or all 25 signed pre-ledger rows was a decision I never surfaced — the
  operator chose all 25 signed (2 unsigned excluded, an I0b finding). **(3) The correction that also
  skipped the source.** My first draft of this entry called 14 "never derived … a guess dressed as a
  prediction" — but it *was* derived and prod-verified in the incident record; I wrote the correction
  without re-reading the primary source it was about. *Lesson: a count carries a denominator — check
  which set it belongs to before quoting it, and never present a subset as the total; the choice
  between subsets is the operator's, not one to settle by picking a number. And a correction is a
  claim too: verify it against the same primary source before writing it down.*
- **2026-09-16 — two granted scopes that enforce nothing.** Mapping the three Meridian prod key
  scope-sets to endpoints for the rotation-v2 E2E, `require_scope(request, "cgr:read")` and
  `require_scope(request, "calibration:write")` appear on **zero** cloud routes — both scopes are in the
  vocabulary and granted to real prod keys, but neither gates any request (`cgr:read` reads are
  tenant-scoped or gated on `decisions:read`; `calibration:write` may be enforced deeper than the route
  layer, per its docstring, but not via `require_scope`). A granted scope that gates nothing is a *false
  affordance* — it reads as authority in `list`/audit output while conferring or restricting nothing at
  the check. *Lesson: presence in the scope vocabulary + a grant on a key is not evidence the scope gates
  anything; grep `require_scope` for the exact string before trusting (or advertising) a scope's authority.*
- **2026-09-16 — the MCP erasure certificate that was never minted.** The MCP `delete` tool has a
  "Phase 4: Mint Erasure Certificate" block (`server/mcp.py`) that returned `"certificate": null` on
  **every** call — it never once minted. Four independent bugs, each caught by a `try/except` that logged
  a *warning* and continued: `issue_certificate(content=…)` (the kwarg is `fact_content=`),
  `signing_identity` passed as the 2nd positional (the `decision_trail` slot), and `cert.content_hash` /
  `cert.signing_key_id` (the fields are `fact_content_hash` / there is no `signing_key_id`); the verify
  block additionally called `get_certificate_for_fact` (no such method — it is `get_by_fact`). So a
  confident label ("Mint … Certificate") sat over a path that had never worked, presenting `null` as if
  minting were merely optional. *Lesson: a `try/except` that logs a warning and continues can hide a
  feature that has never once succeeded — assert the happy path in a test, or the label lies. (Fixed:
  correct kwargs/fields/method + wire the ledger; full MCP issuance still needs the MCP server to wire a
  ledger, tracked separately.)*
- **2026-09-16 — certificate coverage was an asserted default, not a verification.** An erasure
  certificate's `coverage` is `{store: status}` — the record of *whether the erased fact is really gone
  from each subsystem*. But no caller ever computes coverage: `issue_certificate` **auto-defaults** omitted
  coverage to `{"primary": "absent"}` (`erasure_proof.py`), and every caller omits it. So every certificate
  ever issued asserts one subsystem is clean **without probing it** — `absent` here means "we defaulted to
  claiming absence", not "we looked and it was gone". There is no code anywhere that reads a store after a
  scrub to confirm the fact is absent. Two downstream overstatements followed: (1) the whitepapers'
  "Erasure cascade ✅ VALIDATED" presented this as a verified multi-subsystem cascade when it was a
  single asserted default (0013 already narrowed the *ledger* half of that claim; this narrows the
  *coverage* half); (2) the I0c backfill of pre-ledger certificates wrote ledger rows carrying that same
  default/empty coverage — the backfilled rows are no more coverage-verified than the originals, and must
  not be read as evidence of a probed cascade. *Lesson: a field that is only ever populated by a default is
  a declaration, not a measurement — `absent` with no probe is `unverified` wearing a clean label; before
  advertising "verified" coverage, grep for the code that actually reads the store, and if there is none,
  say `unverified`. (Spec fix: decision 0014 names `unverified` and forbids the fabricated default;
  enforcement = I0b stage-1 coverage probe.)*
- **2026-09-16 — the test mocked the API it wished existed.** `test_mcp_governance` was the only
  coverage over the MCP erasure-minting path, and it PASSED for the whole time that path minted nothing.
  It passed because its mocks were written to the *broken* code's shape — `content_hash`,
  `signing_key_id`, a `str` signature, `get_certificate_for_fact` — none of which are the real
  `ErasureProofService` API (`fact_content_hash`, no `signing_key_id`, `bytes` signature, `get_by_fact`).
  The test and the code agreed with each other and disagreed with reality: a green suite over an API that
  did not exist. Fixing the code (#156) *broke the test* (`str.hex()` AttributeError, MagicMock not JSON
  serializable) — which is how we learned the test had been mocking a fiction. *Lesson: a mock encodes a
  claim about a real interface; when the mock and the code are edited together to match, the test proves
  only their mutual consistency, never that either matches the dependency. Assert against the real object
  (or a shared fake built from its actual signature) at least once, or the mock drifts into fan-fiction.
  Corollary to "the certificate that was never minted": the same commit that hid the bug in a swallowed
  warning also had a test blessing it.*

