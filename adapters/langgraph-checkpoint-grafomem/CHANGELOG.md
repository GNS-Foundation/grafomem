# Changelog

All notable changes to `langgraph-checkpoint-grafomem` are documented here.

## [1.2.0] — 2026-11-03

### Added
- **Per-thread crypto-erasure (opt-in).** Pass a `ThreadKeyManager` to `GrafomemSerializer` and
  each thread's checkpoint payloads are encrypted under a per-thread Data Encryption Key (DEK),
  wrapped under a master KEK (`GRAFOMEM_MASTER_KEY`). `delete_thread` destroys the thread's DEK,
  so residual ciphertext outside the wrapper — SQLite freelist/WAL, Postgres dead tuples,
  replicas, snapshots, backups — becomes undecryptable. The erasure receipt's `after` field then
  carries `key_destroyed:<fingerprint>`, so the signed receipt commits to the key destruction.
  Keys live in a pluggable `KeyStore` (default `FileKeyStore` at `GRAFOMEM_THREAD_KEYS` or
  `grafomem_thread_keys.json`; `InMemoryKeyStore` for tests). New exports: `ThreadKeyManager`,
  `KeyStore`, `FileKeyStore`, `InMemoryKeyStore`, `ThreadKeyDestroyed`, `ThreadContextMissing`.

### Changed
- **Opt-in and backward compatible.** Without a `ThreadKeyManager`, behavior is unchanged
  (signed, not encrypted; `after="erased"`), and checkpoints written by 1.0/1.1 still load. The
  content hash is computed over plaintext, so the receipt's content binding is unchanged by
  encryption.

### Known limitation
- With encryption on, a **cross-thread `list()`** (no `thread_id` in scope) raises
  `ThreadContextMissing` — list per thread. This is deliberate: one decryption context cannot
  span threads with per-thread keys.

### Scope (unchanged honesty)
- Key-destruction crypto-erasure, not a guarantee the physical ciphertext bytes were overwritten.
  Assumes the KEK and key store are managed correctly; losing the KEK makes threads unrecoverable.
- Encryption stays inside the adapter (the signed `.gfm` blob); the core `grafomem` CSO wire
  format is unchanged. See ADR 0015.

## [1.1.0] — 2026-11-03

### Added
- **Append-only, hash-chained erasure log.** Every `delete_thread` / `adelete_thread`
  now appends a chained entry to a durable `ReceiptLog`. Each entry links to the previous
  by hash, so reordering or silently dropping an entry is detectable, and the receipt
  signature inside each entry catches forgery. A thread that was never deleted has no
  entry — so an auditor can distinguish a real deletion from a `delete_thread` that was
  never called. `FileReceiptLog` (JSONL) persists across process restarts;
  `InMemoryReceiptLog` is available for tests. `verify_log(entries, trusted_keys)` checks
  the chain and every signature end to end. New `GrafomemCheckpointSaver.audit_log()`.

### Changed (behavior — read before upgrading)
- **The erasure receipt now binds the thread's content.** The signed `before` field is a
  SHA-256 digest of the thread's entire checkpoint history as it stood immediately before
  deletion (previously the literal string `"thread_data"`). The receipt now commits to the
  specific data that was erased, verifiable with the public key and no database access. A
  thread with no checkpoints yields the sentinel `EMPTY_THREAD_DIGEST`. Code that asserted
  `receipt.before == "thread_data"` must be updated; `receipt.verify(public_key)` is
  unchanged.
- **Default erasure log file.** When no `receipt_log` is passed, the saver writes the log
  to `grafomem_receipts.jsonl` in the working directory (override with the
  `GRAFOMEM_RECEIPT_LOG` env var, or pass a `ReceiptLog`). The file is created on the first
  erasure, not at construction. Pass `InMemoryReceiptLog()` to opt out of disk entirely.

### Still out of scope (stated plainly)
- The receipt is tamper-evidence and non-repudiation for the erase transition, bound to a
  key and timestamp — **not** a claim of media-level unrecoverability. The inner store's
  WAL, dead tuples, replicas, snapshots and backups are outside this wrapper's control.
  Per-thread crypto-erasure (a per-thread key destroyed on delete, with the receipt
  committing to key destruction) is the planned next step to cover residual copies.

## [1.0.1]
- Published on PyPI. (Packaging/patch release.)

## [1.0.0]
- Initial release: `GrafomemSerializer` (Ed25519-signed `.gfm` checkpoints, verified on
  load) and `GrafomemCheckpointSaver` (wraps an existing saver; emits a signed erasure
  receipt on `delete_thread`).
