# langgraph-checkpoint-grafomem

A drop-in LangGraph checkpointer that wraps the saver you already use and adds two things:

- **every checkpoint is Ed25519-signed** (tamper-evident — a modified checkpoint fails verification on load), and
- **`delete_thread` emits a signed erasure receipt**, appended to an append-only, hash-chained log.

It's **Fair Source** — free to self-host and modify, your own signing keys, no cloud dependency.

```bash
pip install langgraph-checkpoint-grafomem
```

## Usage

It wraps your existing saver; nothing else about how you build changes.

```python
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver
from grafomem_checkpoint import GrafomemSerializer, GrafomemCheckpointSaver

conn = sqlite3.connect("memory.db", check_same_thread=False)
inner = SqliteSaver(conn, serde=GrafomemSerializer(private_key, key_id=key_id, trusted_keys=trusted_keys))
saver = GrafomemCheckpointSaver(inner)
graph = builder.compile(checkpointer=saver)

# right to be forgotten, with proof:
saver.delete_thread("user-123")
receipt = saver.last_receipt("user-123")
assert receipt.verify(public_key)   # signed proof of the erasure transition
print(receipt.to_json())
```

## The erasure receipt

On `delete_thread`, the receipt's signed `before` field is a **SHA-256 digest of the thread's
entire checkpoint history as it stood immediately before deletion** — so it commits to the
specific data that was erased, not merely to the fact that an erase happened. Anyone with the
public key can verify it, no database access required.

## The append-only erasure log

Every erasure is appended to a hash-chained `ReceiptLog`. Each entry links to the previous one by
hash, so reordering or silently dropping an entry is detectable, and the signature inside each
entry catches forgery. A thread that was never deleted simply has no entry — so **an auditor can
distinguish a real deletion from a `delete_thread` that was never called.**

```python
from grafomem_checkpoint import FileReceiptLog, verify_log

saver = GrafomemCheckpointSaver(inner, receipt_log=FileReceiptLog("receipts.jsonl"))
# ... delete some threads ...

entries = saver.audit_log()                 # all erasures, in append order, survives restart
ok, problems = verify_log(entries, {key_id: public_key})
assert ok, problems
```

The default log is a `FileReceiptLog` at `grafomem_receipts.jsonl` (override with the
`GRAFOMEM_RECEIPT_LOG` env var, or pass your own `ReceiptLog`). The file is created on the first
erasure. Pass `InMemoryReceiptLog()` to opt out of disk entirely.

## Per-thread crypto-erasure (opt-in)

The receipt and log above prove an erasure *happened*, but the inner store's residual copies
(WAL, dead tuples, replicas, backups) are outside the wrapper. To cover those, turn on per-thread
encryption: each thread's checkpoints are encrypted under a per-thread key, and `delete_thread`
**destroys that key** — so residual ciphertext becomes undecryptable, and the receipt commits to
the key destruction.

```python
from grafomem_checkpoint import GrafomemSerializer, GrafomemCheckpointSaver, ThreadKeyManager

# master key (KEK) wraps the per-thread keys; keep it safe, losing it makes threads unrecoverable
km = ThreadKeyManager()  # reads GRAFOMEM_MASTER_KEY (32-byte hex); keys in GRAFOMEM_THREAD_KEYS
serde = GrafomemSerializer(private_key, key_id=key_id, trusted_keys=trusted_keys, key_manager=km)
inner = SqliteSaver(conn, serde=serde)
saver = GrafomemCheckpointSaver(inner)

saver.delete_thread("user-123")
receipt = saver.last_receipt("user-123")
assert receipt.after.startswith("key_destroyed:")   # signed commitment to the destroyed key
```

Encryption is opt-in (no `key_manager` → unchanged signed-but-plaintext behavior), backward
compatible (1.0/1.1 checkpoints still load), and stays inside the adapter — the core `.gfm` wire
format is untouched. One constraint: with encryption on, a cross-thread `list()` raises
`ThreadContextMissing`; list per `thread_id`.

## Separate signer (opt-in, 1.3.0)

By default the saver signs receipts with the same key that signs checkpoints, and appends to a
log file it opens itself — so a buggy or compromised process can sign a receipt that verifies.
To take both the key and the log out of the agent's hands, run a **ledger signer** as a separate
process and point the saver at it:

```bash
# the signer: holds the receipt key and the only write handle to the log
GRAFOMEM_SIGNER_KEY=$(python -c "import secrets;print(secrets.token_hex(32))") \
GRAFOMEM_SIGNER_TOKEN=... grafomem signer --port 8644 --log receipts.jsonl
```

```python
from grafomem_checkpoint import RemoteReceiptSigner

saver = GrafomemCheckpointSaver(
    inner, receipt_signer=RemoteReceiptSigner("http://127.0.0.1:8644", token="..."))
```

The saver then holds **no receipt key and no log handle**: it hands the signer a claim
(`before`, `after`, `thread_id`), the signer signs with its own key and appends. Receipts carry
the signer's `key_id`; `verify_log` and `Receipt.verify` work unchanged against the signer's
public key, which the saver learns at start-up. Entries record the caller and mark the `before`
digest as **caller-asserted** — the signer cannot look inside your store, so what this buys is
custody separation, not independent verification of the claim.

Failure semantics are **fail closed**: `delete_thread` checks the signer *before* deleting and
refuses (`SignerUnavailable`) if it is down. If the signer fails between the delete and the sign,
the thread is erased and unreceipted: a marker goes to `grafomem_receipts_pending.jsonl`
(`GRAFOMEM_RECEIPT_PENDING`) and `ReceiptPending` is raised; `saver.flush_pending()` retries, and
the eventual entry shows `caller_erased_at` earlier than `recorded_at`. Pass
`allow_unreceipted=True` to record the marker and continue instead of raising. The old
behaviour is `LocalReceiptSigner` and remains the default.

## What it proves — and what it doesn't

The receipt is **tamper-evidence and non-repudiation for the erase transition**, bound to a key
and a timestamp, with the thread's pre-deletion content bound in. The honest statement is *"we can
prove this thread was erased on this date, bound to what was there, signed and independently
verifiable."*

On its own it is **not** a claim of media-level unrecoverability: the inner store's WAL, freelist
pages, dead tuples before VACUUM, replicas, snapshots and backups are outside this wrapper's
control. Turning on per-thread crypto-erasure (above) covers those residual copies by destroying
the thread's key on delete — but that is still key-destruction, not a guarantee the physical
ciphertext bytes were overwritten, and it depends on the KEK and key store being managed correctly.

## License

Apache-2.0. © Ulissy s.r.l. · [grafomem.com](https://grafomem.com)
