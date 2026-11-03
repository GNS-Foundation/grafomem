# 0015 — Checkpointer per-thread crypto-erasure stays adapter-local (no CSO wire-format change)

**Status:** accepted 2026-11-03 · **Relates to:** [0013](0013-erasure-certificate-issuance-requires-ledger-entry.md),
[0014](0014-erasure-coverage-status-vocabulary.md) (erasure semantics); `langgraph-checkpoint-grafomem` v1.2.0.

## Context

`langgraph-checkpoint-grafomem` v1.1.0 made erasure receipts tamper-evident, content-bound, and
append-only-logged, but a receipt proves an erase *happened*, not that residual copies are gone.
Community feedback (forum.langchain.com #4137, capitaharlock) correctly noted that SQLite
freelist/WAL, Postgres dead tuples, replicas, snapshots and backups survive outside the wrapper,
and proposed a per-thread encryption key destroyed on delete, with the receipt committing to the
destruction. The question: **where does that encryption live** — in the shared core `grafomem`
CSO/`.gfm` format, or inside the adapter?

## Decision

**Adapter-local.** Per-thread crypto-erasure is implemented entirely within
`langgraph-checkpoint-grafomem`: the serializer encrypts the already-serialized checkpoint bytes
(Fernet, per-thread DEK wrapped under `GRAFOMEM_MASTER_KEY`) and wraps the **ciphertext** as the
blob inside the existing signed CSO. `grafomem/cso.py` — the `.gfm` wire format, which the Cloud
also uses and a deployed verifier depends on — is **not** changed. The `Receipt` dataclass in
`grafomem.runtime` is **not** changed either: key destruction is recorded in the existing signed
`after` field as `key_destroyed:<fingerprint>`.

Supporting choices:
- **Content hash stays over plaintext.** `compute_content_hash` hashes the plaintext bytes, so the
  receipt's content binding (0014-style "what was erased") and LangGraph's keying are unaffected by
  encryption. Encryption is strictly a storage concern.
- **Opt-in, backward compatible.** Encryption is active only when a `ThreadKeyManager` is supplied.
  A `meta["enc"]` flag on the CSO header (signed) distinguishes encrypted from plaintext blobs, so
  checkpoints written by 1.0/1.1 still load.
- **Cross-thread `list()` fails closed.** With per-thread keys, one decryption context cannot span
  threads; a `list()` with no `thread_id` in scope raises `ThreadContextMissing` rather than
  silently returning undecryptable or wrong data.

## Why not change the core CSO format

Changing `cso.py` would (a) alter an artifact the Cloud's verifiers consume — a deployed-verifier
change requiring explicit approval and careful versioning (per the signing/merge discipline), and
(b) couple an OSS checkpointer feature to the core protocol for no benefit, since encrypting the
blob *inside* the signed envelope achieves the same guarantee without touching the format. Keeping
it adapter-local is the smaller blast radius and the correct placement per ADR-0005 (standard/OSS
adapter content).

## Consequences

- The guarantee is **key-destruction crypto-erasure**: residual ciphertext becomes undecryptable
  once the per-thread DEK is destroyed. It is **not** a claim that physical ciphertext bytes were
  overwritten, and it assumes the KEK and key store are managed correctly (losing the KEK makes
  threads unrecoverable — stated plainly in the README/CHANGELOG, consistent with the
  "declared ≠ behaviour" discipline of 0013/0014).
- A non-vacuity test is the acceptance gate: a retained ciphertext copy decrypts before
  `delete_thread` and raises `ThreadKeyDestroyed` after.
- If a future need requires encryption at the core (e.g. Cloud GMP payloads), that is a separate
  decision with its own ADR and verifier-approval; it is explicitly out of scope here.
