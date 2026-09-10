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

