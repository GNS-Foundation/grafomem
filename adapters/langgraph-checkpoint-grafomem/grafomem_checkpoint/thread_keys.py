"""
Per-thread crypto-erasure for the LangGraph checkpointer.

Each thread's checkpoint payloads are encrypted under a per-thread Data Encryption Key (DEK),
wrapped under a master Key Encryption Key (KEK). `delete_thread` destroys the thread's DEK, so
residual ciphertext that survives outside the wrapper — SQLite freelist/WAL, Postgres dead
tuples, replicas, snapshots, backups — becomes undecryptable. The erasure receipt commits to
that key destruction (its `after` field carries the destroyed key's fingerprint).

Trust model and limits, stated plainly: this is key-destruction crypto-erasure, not a
guarantee that the physical ciphertext bytes were overwritten. It assumes the KEK
(GRAFOMEM_MASTER_KEY) and the key store are managed correctly; losing the KEK makes every
thread unrecoverable (that is the point, but it is also an operational hazard). Encryption is
opt-in — it is active only when a ThreadKeyManager is supplied to GrafomemSerializer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from contextvars import ContextVar
from typing import Optional, Protocol

from cryptography.fernet import Fernet, InvalidToken

# The saver sets this before delegating to the inner saver, so the serde knows which thread's
# key to use during dumps_typed / loads_typed. None means "no thread in scope".
current_thread_id: ContextVar[Optional[str]] = ContextVar("grafomem_current_thread_id", default=None)


class ThreadKeyDestroyed(Exception):
    """Raised when a thread's key has been destroyed (crypto-erased) and its data can no
    longer be decrypted."""


class ThreadContextMissing(Exception):
    """Raised when an encrypted checkpoint must be read but no thread_id is in scope — e.g. a
    cross-thread list(). List per thread_id instead."""


# --- Key store ---------------------------------------------------------------

class KeyStore(Protocol):
    """Stores wrapped per-thread DEKs. A destroyed key is tombstoned, not silently recreated."""

    def get(self, thread_id: str) -> Optional[dict]: ...
    def put(self, thread_id: str, record: dict) -> None: ...
    def tombstone(self, thread_id: str, fingerprint: str) -> None: ...


class InMemoryKeyStore:
    """Non-durable key store for tests/ephemeral use."""

    def __init__(self) -> None:
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, thread_id: str) -> Optional[dict]:
        with self._lock:
            r = self._d.get(thread_id)
            return dict(r) if r else None

    def put(self, thread_id: str, record: dict) -> None:
        with self._lock:
            self._d[thread_id] = dict(record)

    def tombstone(self, thread_id: str, fingerprint: str) -> None:
        with self._lock:
            self._d[thread_id] = {"destroyed": True, "fp": fingerprint}


class FileKeyStore:
    """Durable key store backed by a single JSON object file:
    {thread_id: {"wrapped": <hex>, "fp": <hex>, "destroyed": bool}}.

    The wrapped DEK is destroyed by deletion (replaced with a tombstone that keeps only the
    fingerprint), so the key material is gone even though the thread_id is still recorded.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as fh:
            txt = fh.read().strip()
        return json.loads(txt) if txt else {}

    def _write(self, data: dict) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def get(self, thread_id: str) -> Optional[dict]:
        with self._lock:
            return self._read().get(thread_id)

    def put(self, thread_id: str, record: dict) -> None:
        with self._lock:
            data = self._read()
            data[thread_id] = record
            self._write(data)

    def tombstone(self, thread_id: str, fingerprint: str) -> None:
        with self._lock:
            data = self._read()
            data[thread_id] = {"destroyed": True, "fp": fingerprint}
            self._write(data)


# --- Key manager -------------------------------------------------------------

class ThreadKeyManager:
    """Per-thread DEKs wrapped under a master KEK, with destroy-on-delete.

    master_key_hex: >=64 hex chars (>=32 bytes). Defaults to env GRAFOMEM_MASTER_KEY.
    key_store: defaults to a FileKeyStore at env GRAFOMEM_THREAD_KEYS or
    'grafomem_thread_keys.json'.
    """

    def __init__(self, master_key_hex: Optional[str] = None, key_store: Optional[KeyStore] = None):
        master_key_hex = master_key_hex or os.environ.get("GRAFOMEM_MASTER_KEY", "")
        if not master_key_hex or len(master_key_hex) < 64:
            raise ValueError(
                "ThreadKeyManager requires a master key of at least 32 bytes (64 hex chars); "
                "set GRAFOMEM_MASTER_KEY or pass master_key_hex."
            )
        kek_bytes = bytes.fromhex(master_key_hex)[:32]
        self._kek = Fernet(base64.urlsafe_b64encode(kek_bytes))
        if key_store is None:
            key_store = FileKeyStore(os.environ.get("GRAFOMEM_THREAD_KEYS", "grafomem_thread_keys.json"))
        self._store = key_store
        self._cache: dict[str, Fernet] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _fingerprint(raw_dek: bytes) -> str:
        return hashlib.sha256(raw_dek).hexdigest()[:16]

    def get_key(self, thread_id: str) -> Fernet:
        """Return the thread's Fernet DEK, creating one on first use. Raises
        ThreadKeyDestroyed if the thread was crypto-erased."""
        with self._lock:
            cached = self._cache.get(thread_id)
            if cached is not None:
                return cached
            rec = self._store.get(thread_id)
            if rec is not None and rec.get("destroyed"):
                raise ThreadKeyDestroyed(f"Thread {thread_id} has been crypto-erased.")
            if rec is not None:
                raw = self._kek.decrypt(bytes.fromhex(rec["wrapped"]))
            else:
                raw = Fernet.generate_key()
                wrapped = self._kek.encrypt(raw).hex()
                self._store.put(thread_id, {"wrapped": wrapped, "fp": self._fingerprint(raw), "destroyed": False})
            dek = Fernet(raw)
            self._cache[thread_id] = dek
            return dek

    def fingerprint(self, thread_id: str) -> Optional[str]:
        rec = self._store.get(thread_id)
        return rec.get("fp") if rec else None

    def destroy_key(self, thread_id: str) -> str:
        """Crypto-erase a thread: delete its wrapped DEK, leaving a tombstone. Returns the
        destroyed key's fingerprint (or 'none' if the thread had no key)."""
        with self._lock:
            self._cache.pop(thread_id, None)
            rec = self._store.get(thread_id)
            if rec is None:
                return "none"
            if rec.get("destroyed"):
                return rec.get("fp", "none")
            fp = rec.get("fp", "none")
            self._store.tombstone(thread_id, fp)
            return fp
