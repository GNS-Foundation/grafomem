"""
Append-only, hash-chained log of erasure receipts.

Why this exists
---------------
An erasure receipt proves that a `delete_thread` *was signed* — it is tamper-evidence
over the claimed transition, not proof the bytes are gone. On its own, a single in-memory
receipt cannot answer an auditor's real question: *"can you distinguish a thread that was
deleted from one where delete_thread was never called?"*

A durable, append-only log answers exactly that: every erasure appends one entry, each
entry is chained to the previous one by hash, and a missing thread is simply an absence of
an entry. The chain makes reordering or silent removal of an entry detectable; the receipt
signature inside each entry makes forging one detectable. The trust model is the operator's
(the log lives with the operator, like the signing key) — this is an append-only *audit*
record, not a third-party-held ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Iterable, Protocol

from grafomem.runtime import Receipt

GENESIS_HASH = "0" * 64


# --- (de)serialization of a Receipt to a JSON-safe dict ---------------------

def receipt_to_dict(r: Receipt) -> dict:
    return {
        "before": r.before,
        "after": r.after,
        "scope": r.scope,
        "key_id": r.key_id,
        "timestamp": r.timestamp,
        "signature": r.signature.hex(),
    }


def receipt_from_dict(d: dict) -> Receipt:
    return Receipt(
        before=d["before"],
        after=d["after"],
        scope=d["scope"],
        key_id=d["key_id"],
        timestamp=d["timestamp"],
        signature=bytes.fromhex(d["signature"]),
    )


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _entry_hash(prev_entry_hash: str, receipt_dict: dict) -> str:
    return hashlib.sha256(
        (prev_entry_hash + _canonical(receipt_dict)).encode("utf-8")
    ).hexdigest()


def _make_entry(seq: int, prev_entry_hash: str, r: Receipt) -> dict:
    rd = receipt_to_dict(r)
    return {
        "seq": seq,
        "prev_entry_hash": prev_entry_hash,
        "receipt": rd,
        "entry_hash": _entry_hash(prev_entry_hash, rd),
    }


class ReceiptLog(Protocol):
    """Append-only log of erasure receipts."""

    def append(self, receipt: Receipt) -> dict:
        """Append one receipt as a chained entry; return the entry dict."""
        ...

    def entries(self) -> list[dict]:
        """Return all entries in append order."""
        ...


class InMemoryReceiptLog:
    """Append-only, hash-chained log held in memory. Does NOT survive restart —
    use FileReceiptLog for a durable audit log; this is for tests and ephemeral use."""

    def __init__(self) -> None:
        self._entries: list[dict] = []
        self._lock = threading.Lock()

    def append(self, receipt: Receipt) -> dict:
        with self._lock:
            prev = self._entries[-1]["entry_hash"] if self._entries else GENESIS_HASH
            entry = _make_entry(len(self._entries), prev, receipt)
            self._entries.append(entry)
            return entry

    def entries(self) -> list[dict]:
        with self._lock:
            return list(self._entries)


class FileReceiptLog:
    """Append-only, hash-chained log persisted as JSON Lines.

    Each line is one entry: {seq, prev_entry_hash, receipt, entry_hash}. On construction
    the existing file is read so a new process ("restart") continues the same chain rather
    than starting a fresh one — which is what makes the log a durable audit record.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last_hash = GENESIS_HASH
        self._count = 0
        existing = self._read_raw()
        if existing:
            self._count = len(existing)
            self._last_hash = existing[-1]["entry_hash"]

    def _read_raw(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out: list[dict] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def append(self, receipt: Receipt) -> dict:
        with self._lock:
            entry = _make_entry(self._count, self._last_hash, receipt)
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._last_hash = entry["entry_hash"]
            self._count += 1
            return entry

    def entries(self) -> list[dict]:
        with self._lock:
            return self._read_raw()


def verify_log(entries: Iterable[dict], trusted_keys: dict) -> tuple[bool, list[str]]:
    """Verify an append-only receipt log end to end.

    Checks, for every entry: (1) the hash chain (prev_entry_hash links to the previous
    entry's entry_hash, genesis for the first), (2) each entry_hash is the real hash of its
    contents (so a mutated receipt is caught), and (3) the receipt's Ed25519 signature
    verifies against the trusted public key for its key_id.

    Returns (ok, problems). ok is True only if problems is empty.
    """
    problems: list[str] = []
    prev = GENESIS_HASH
    for i, entry in enumerate(entries):
        if entry.get("prev_entry_hash") != prev:
            problems.append(f"entry {i}: broken chain (prev_entry_hash mismatch)")
        rd = entry.get("receipt", {})
        recomputed = _entry_hash(entry.get("prev_entry_hash", ""), rd)
        if recomputed != entry.get("entry_hash"):
            problems.append(f"entry {i}: entry_hash does not match contents (tampered)")
        key_id = rd.get("key_id")
        pub = trusted_keys.get(key_id)
        if pub is None:
            problems.append(f"entry {i}: no trusted key for key_id={key_id!r}")
        else:
            try:
                r = receipt_from_dict(rd)
                if not r.verify(pub):
                    problems.append(f"entry {i}: receipt signature does not verify")
            except Exception as e:  # malformed receipt
                problems.append(f"entry {i}: receipt could not be verified ({e})")
        prev = entry.get("entry_hash", prev)
    return (len(problems) == 0, problems)
