# 0020 — The erasure signer is not the erasure caller

**Status:** proposed 2026-11-10; approved for build by the operator the same day (all forks per
recommendation); **part 1 built** (the signer service + the adapter client, this record); **part 2
pending** (the API becomes a client of the signer; Railway service; key moves). · **Relates to:**
[0015](0015-checkpointer-crypto-erasure-stays-adapter-local.md), [0016](0016-erasure-certificates-are-factual.md),
[0017](0017-hash-chained-erasure-ledger-and-mcp-erasure-service.md), [0019](0019-data-subject-scope-and-crypto-erasure.md).
Trigger: forum.langchain.com #4137, post 3 — the fourth criterion after C1 (content-bound), C2 (chained,
append-only), C3 (key destroyed): **C4, signer ≠ caller.**

## Question

A chained, signed log proves entries were not altered or removed *after* they were written. It cannot
prove the process that wrote them told the truth. Verified at `main` @ `0314699`:

| Surface | Signing key | Log / ledger write handle |
|---|---|---|
| Adapter `GrafomemSaver` (1.2.1) | in-process; the **same** key signs every checkpoint on `put` and every receipt (`saver.py:98-100`) | in-process; `FileReceiptLog` opened for writing by the saver (`:112`) |
| Server (0.5.0) | in the API process (`app.py:1048`) | in the API process, under a separate DB role: `grafomem_ledger` SELECT+INSERT (009), `grafomem_rt` revoked (010), chained (021) |

"Which process holds the signing key and the write handle? If it's the same one that calls
`delete_thread`, a buggy or compromised caller can sign a receipt that verifies, because the signer is
legit." Correct, on both surfaces. 0016's `FactStillPresent` gate narrows what a receipt can lie about;
a compromised process skips the gate.

## Decision

### One separate component, two callers

A **ledger signer** (`grafomem signer`, `aml/cloud/signer.py`): a process that holds the receipt
private key and the only write handle to its chained receipt log, with a bearer-token HTTP surface
(`POST /v1/sign/receipt`, `GET /v1/sign/{health,entries,verify}`). Callers hand it a claim; it signs
with **its** key (its own clock as the receipt timestamp), appends, returns the entry. The entry format
and chain hash are byte-identical to the adapter's `receipt_log.py`, so the adapter's `verify_log` is the
verifier for both. Entry-level annotations (`caller`, `caller_erased_at`, `recorded_at`, `before_basis`)
sit outside the chain hash and are stated as operator context, not attested facts.

### Adapter (1.3.0) — custody separation, stated as such

- **The keys are split.** Checkpoint signing stays in-process (hot path, authenticity of stored
  blobs). The receipt key moves to the signer. `Receipt.key_id` already travels in the signed payload;
  the signer's public key is learned at pre-flight and added to `trusted_keys`, so `Receipt.verify`
  and `verify_log` are unchanged. Pre-1.3.0 receipts (signed with the checkpoint key) stay verifiable.
- `ReceiptSigner` protocol; `LocalReceiptSigner` (the old behaviour, named, still the default);
  `RemoteReceiptSigner` (no key, no log handle, no `_log` attribute to reach for).
- **Honest limit:** the signer cannot read the inner saver; `before` is recorded as `caller_asserted`.
  On this surface C4 is key and log custody, not independent verification.
- **F1, fail closed:** `delete_thread` pre-flights the signer and refuses (`SignerUnavailable`) before
  anything is erased. A failure *after* the delete leaves the thread erased and unreceipted: a local
  pending marker, `ReceiptPending` raised, `flush_pending()` retries, and the eventual entry carries
  `caller_erased_at` earlier than `recorded_at`. `allow_unreceipted=True` records the marker and
  continues. Never a locally-forged stand-in receipt.
- F2: one receipt key per signer deployment. F3: the signer does **not** own the adapter's thread key
  store (off by default; a `RemoteKeyStore` is a later option). F4: bearer token per deployment, the
  caller name recorded in the entry. F5: `verify` is served by the signer.

### Server (part 2, not in this record's build)

The API process becomes a client of the same signer: `POST /v1/erasure/sign` on the ledger role, the
signer reads the fact back itself (0016's gate out of the caller's process), checks the data-subject key
tombstone (0019), signs, appends under the advisory lock (021). The API loses `ERASURE_SIGNING_KEY` and
`GRAFOMEM_LEDGER_URL`; `/v1/erasure/ledger/verify` is proxied read-only. F3 on the server: yes — the
signer owns `tenant_deks` / `data_subject_deks` (it is already the ledger role after 0019 F4). A new
Railway service and the key move are production changes that need the operator's explicit approval
before each step.

## What C4 buys, stated once

*A receipt is signed by a process the erasure caller does not control and appended to a log the caller
cannot write; on the server (part 2) it is issued only after that process has itself observed the fact
gone and the key destroyed. A compromised or buggy caller can delete, or fail to delete; it cannot
manufacture a verifying receipt for either.* Still tamper-evident, never tamper-proof: a compromised
**signer** can lie; its key and host are what remain to be trusted — a smaller set than before.

## Consequences

- `tests/test_ledger_signer.py` (core) and the adapter's `tests/test_remote_signer.py`: a receipt the
  saver forges with its own key under the signer's `key_id` is rejected by `verify_log`; a signer that is
  down refuses the delete before anything is erased; a signer that dies after the delete leaves a visible
  pending marker that `flush_pending` drains with the gap on record; a tampered signer log fails
  verification; wrong token → 401; the adapter's verifier verifies the signer's entries unchanged.
- The adapter's `audit_log()` now reads through the signer; passing both `receipt_log` and
  `receipt_signer` raises.
- The forum reply (post 4) goes out after this ships, per the operator.
