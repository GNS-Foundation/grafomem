# 0019 — Data-subject scope in GMP; per-data-subject crypto-erasure in Cloud

**Status:** proposed 2026-11-06 (spec-first: this record and spec §7.7 land before any code) ·
**Relates to:** [0014](0014-erasure-coverage-status-vocabulary.md) (coverage vocabulary),
[0015](0015-checkpointer-crypto-erasure-stays-adapter-local.md) (the LangGraph adapter's per-thread
crypto-erasure — the design this transplants), [0016](0016-erasure-certificates-are-factual.md),
[0017](0017-hash-chained-erasure-ledger-and-mcp-erasure-service.md) (ledger), and
[0012](0012-product-code-resident-in-the-foundation-repo-by-exception.md) (why Cloud code is here).
Item 3 of the erasure-honesty audit (2026-11-03).

## Question

After 0016/0017 a per-fact erasure is factual and the ledger is chained. Two things remain untrue of
the Cloud's "right to erasure" story, both verified at `main` @ `ac54d57`:

1. **A per-fact delete destroys no key.** Content and metadata are encrypted under a **per-tenant**
   DEK (`postgres_gmp.py:343-355`, `tenant_key_manager.py`). `delete(ref)` removes the row and marks
   the embedding `erasure_pending` (`postgres_gmp.py:516-521`). Residual ciphertext in WAL, dead
   tuples, replicas and backups stays decryptable under the tenant DEK for as long as that tenant
   exists. Only `destroy_tenant_key` (admin) destroys anything — all of a tenant, or nothing.
2. **There is no data subject.** `WriteOptions` carries `tenant_id`, `region`, free `metadata`.
   "Delete everything about this person" is a caller-side scan over whatever convention the caller
   put in metadata — no read-path guarantee, no certificate for the scope, nothing the conformance
   suite can check.

Also found: `TenantKeyManager`'s docstring says the wrapped DEKs live in "an external key store DB,
separate from the primary data DB"; in deployment they are created on the primary `db_url`
(`app.py:959`). A restore of the primary database restores destroyed keys. Recorded here as a
correction; the docstring is fixed with the build.

## Decision

### Spec (GMP v0.2 §7.7) — `DATA_SUBJECT`, an optional extension

A twelfth capability flag, `DATA_SUBJECT`, gating `WriteOptions.data_subject_id`,
`Memory.data_subject_id`, and `erase_data_subject(data_subject_id, opts) → [ref]`. Two-sided on the
read path (no leak of the subject's facts; no over-erasure of anyone else's), idempotent, scope fixed
at write time, superseded versions erased with the head. A store that does not claim the flag MUST
raise `CapabilityNotSupported` when the option is set. Implies `HARD_DELETE`. The spec says nothing
about *how* storage makes content unrecoverable — that is the certificate layer's claim.

**Name.** The memo that preceded this record proposed `SUBJECT_SCOPED` / `subject_id`. In the spec
`subject` already means the subject of the fact triple (§1.1, the `fact_id` formula), so the name was
changed to the GDPR term before anything was written: `DATA_SUBJECT` / `data_subject_id`. Recorded
so the memo's name is not mistaken for the decision.

**Conformance.** §8.3 gains a two-direction row. The workload is a *constructed* test like the
provenance rows: write facts under two data subjects and none, erase one, check all three
surfaces (`retrieve`, `audit`, `exists`) on both sides. Lands with the build (PR B), not here.

### Cloud (the seven forks, decided 2026-11-06 — "go on all bold")

| # | Fork | Decision |
|---|---|---|
| F1 | What is a data subject | `data_subject_id` on `WriteOptions`, an indexed nullable column on `memories`, under the `DATA_SUBJECT` flag — not a metadata convention |
| F2 | Key hierarchy | KEK → tenant DEK → **random** per-data-subject DEK wrapped under the tenant DEK (`data_subject_deks(tenant_id, data_subject_id, wrapped_dek, destroyed_at)`). Not HKDF-derived: a derived key cannot be destroyed on its own. Facts without a data subject stay under the tenant DEK |
| F3 | Embeddings | Cannot be encrypted and searched. On data-subject erasure, the subject's `memory_embeddings` rows are hard-deleted **synchronously** (no sweeper window). The certificate says vectors are covered by deletion, not key destruction. Per-fact delete keeps the sweeper |
| F4 | Where wrapped keys live | Move `tenant_deks` and `data_subject_deks` to the ledger role/URL (`GRAFOMEM_LEDGER_URL`) now; the restore-scrub replays `data_subject_key_destroyed` ledger entries and re-destroys any key a restore brought back. **Logical** separation: in the current Railway deployment the ledger role is on the same Postgres instance. Physical separation (a second Postgres service) and an external KMS are the operator's later calls — **default recorded: not now** |
| F5 | Certificate commitment | `coverage["data_subject_key"] = "destroyed:<key fingerprint>"` (per fact; `not_applicable` for facts under the tenant DEK) plus a ledger entry type `data_subject_key_destroyed` {tenant, data-subject fingerprint = hash of the id, key fingerprint, refs}. No certificate schema change. `verify_certificate` checks the fingerprint against the key store's tombstone → `enforced` only if the key is really gone |
| F6 | API | `POST /v1/erasure/data-subject {data_subject_id, requested_by, legal_basis}` (scope `erasure:execute`): existing per-fact machinery for every fact (read-before-delete, 409-on-present, one certificate each), then destroy the data-subject DEK, hard-delete embeddings, append the ledger entry; returns `{data_subject_fingerprint, key_destroyed, certificates, facts}`. MCP `erase_data_subject` only when an erasure service is present. Per-fact `delete` never destroys a key |
| F7 | Rollout | Opt-in per tenant; applies to writes carrying `data_subject_id` from then on. Older rows stay under the tenant DEK and are deletion-only; the certificate says per fact which it was. Lazy re-encryption of old rows is a later batch job |

### The claim this buys, stated once

*For a tenant with data-subject encryption on, erasing a data subject deletes their facts,
hard-deletes their embeddings and destroys the per-data-subject key, so residual ciphertext of their
content and metadata in WAL, dead tuples, replicas and backups is undecryptable; each certificate
commits to the key fingerprint and the ledger records the destruction; a restore re-destroys keys
from the ledger. Not covered: embedding vectors in dead tuples and backups (deletion, not key
destruction); facts written before the feature was enabled (deletion only); the KEK and key store
being managed correctly.* Tamper-evident, never tamper-proof.

## Consequences

- **Spec lands first.** This PR carries §7.7, the enum value, the two dataclass fields, the
  interface comment contract and the capability-count pins (11 → 12). No backend claims the flag
  yet; no behaviour changes. Everything in "Cloud" above is **PR B**, built only after this is merged.
- **Migration (PR B, additive):** `memories.data_subject_id TEXT` + index; `data_subject_deks` and
  `tenant_deks` under the ledger role; a new ledger `entry_type`. It touches the deployed verifier
  and **requires the operator's explicit approval before it runs on Railway**, as 021 did.
- **Conformance (PR B):** the `DATA_SUBJECT` constructed test; the reference SQLite backend claims
  the flag so the suite has a passing and a failing (flipped) implementation.
- **Docs (after PR B):** the erasure page's "What is not covered" list changes from "per-record
  delete destroys no key" to the paragraph above.
- Out of scope, named: re-scoping a fact to another data subject; `data_subject_id` as a retrieval
  filter; physical key-store separation; external KMS.
