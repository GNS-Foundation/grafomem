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

## What it proves — and what it doesn't

The receipt is **tamper-evidence and non-repudiation for the erase transition**, bound to a key
and a timestamp, with the thread's pre-deletion content bound in. The honest statement is *"we can
prove this thread was erased on this date, bound to what was there, signed and independently
verifiable."*

It is **not** a claim of media-level unrecoverability. The inner store's WAL, freelist pages, dead
tuples before VACUUM, replicas, snapshots and backups are outside this wrapper's control.
Per-thread crypto-erasure — a per-thread encryption key destroyed on delete, with the receipt
committing to that destruction so residual ciphertext becomes undecryptable — is the planned next
step to cover those residual copies.

## License

Apache-2.0. © Ulissy s.r.l. · [grafomem.com](https://grafomem.com)
