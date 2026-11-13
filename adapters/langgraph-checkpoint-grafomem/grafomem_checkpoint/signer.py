"""
Receipt signers — who signs an erasure receipt, and who may write the log (ADR-0020, C4).

Through 1.2.x the saver signed receipts with the SAME Ed25519 key that signs every
checkpoint, and appended to a log file it opened itself. A buggy or compromised caller could
therefore produce a receipt that verifies, because the signer was legitimate. The chain
detects tampering after the fact; it does not detect a legitimately signed lie.

Two signers:

- `LocalReceiptSigner` — today's behaviour, now named. The receipt key and the log live in
  the saver's process. Receipts carry `key_id` so a verifier can tell which key signed.
- `RemoteReceiptSigner` — the saver holds NO receipt key and NO log handle. It posts the
  claim to a ledger signer (`grafomem signer`, or the Cloud's) over HTTPS with a bearer
  token; the signer signs with ITS key, appends to ITS log, returns the entry. The signer's
  public key is learned at pre-flight and added to the serializer's trusted keys so
  `Receipt.verify` and `verify_log` work unchanged.

Honest limit: a remote signer cannot look inside the inner saver. On this surface C4 is
custody separation (key and log out of the caller's hands); the `before` digest is recorded
by the signer as caller-asserted. Independent verification of the claim is the server-side
signer's job.

Failure semantics (F1, fail closed): `delete_thread` calls `preflight()` BEFORE deleting —
a signer that is down means the delete is refused (`SignerUnavailable`). If the signer
fails between the delete and the sign (rare), the erasure is real and unreceipted: the saver
writes a local `pending` marker and raises `ReceiptPending`; `flush_pending()` retries, and
the eventual entry carries `caller_erased_at` ≠ `recorded_at`, visibly. With
`allow_unreceipted=True` the saver records the marker and returns None instead of raising.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional, Protocol

from grafomem.runtime import Receipt

from .receipt_log import ReceiptLog, InMemoryReceiptLog


class SignerUnavailable(RuntimeError):
    """The receipt signer cannot be reached: the erasure is refused (fail closed)."""


class ReceiptPending(RuntimeError):
    """The thread WAS erased but the receipt could not be signed/recorded; a pending marker
    was written. Call `saver.flush_pending()` once the signer is back."""

    def __init__(self, thread_id: str, marker_path: str, cause: Exception) -> None:
        super().__init__(f"thread {thread_id!r} erased but unreceipted: {cause} (pending marker at {marker_path})")
        self.thread_id = thread_id
        self.marker_path = marker_path
        self.cause = cause


class ReceiptSigner(Protocol):
    key_id: str

    def preflight(self) -> None:
        """Raise SignerUnavailable if a receipt could not be issued right now."""
        ...

    def sign_and_record(self, before: str, after: str, scope: str,
                        caller_erased_at: str | None = None) -> tuple[Receipt, dict]:
        """Sign the claim and append it; return (receipt, log entry)."""
        ...

    def public_key(self):
        """The verifying key for `key_id`."""
        ...

    def entries(self) -> list[dict]: ...


class LocalReceiptSigner:
    """Same-process signer (pre-1.3.0 behaviour, named). Holds the key and the log."""

    def __init__(self, private_key, key_id: str, log: ReceiptLog | None = None) -> None:
        self._priv = private_key
        self.key_id = key_id
        self._log = log if log is not None else InMemoryReceiptLog()
        self.kind = "local"

    def preflight(self) -> None:
        return None

    def public_key(self):
        return self._priv.public_key()

    def sign_and_record(self, before, after, scope, caller_erased_at=None):
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = f"{before}|{after}|{scope}|{self.key_id}|{timestamp}".encode("utf-8")
        rcpt = Receipt(before=before, after=after, scope=scope, key_id=self.key_id,
                       timestamp=timestamp, signature=self._priv.sign(payload))
        entry = self._log.append(rcpt)
        return rcpt, entry

    def entries(self) -> list[dict]:
        return self._log.entries()


class RemoteReceiptSigner:
    """Posts claims to a ledger signer. Holds no private key and no log handle."""

    def __init__(self, url: str, token: str, *, timeout: float = 5.0, caller: str | None = None) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self.caller = caller or "langgraph-checkpoint-grafomem"
        self.key_id: str = ""          # learned at preflight
        self._public_key = None
        self.kind = "remote"

    # -- transport ---------------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        import urllib.request
        import urllib.error
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise SignerUnavailable(f"signer {path} → HTTP {e.code}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SignerUnavailable(f"signer {path} unreachable: {e}") from e

    def preflight(self) -> None:
        h = self._request("GET", "/v1/sign/health")
        if not h.get("ok") or not h.get("key_id") or not h.get("public_key"):
            raise SignerUnavailable(f"signer health malformed: {h}")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        self.key_id = h["key_id"]
        self._public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(h["public_key"]))

    def public_key(self):
        if self._public_key is None:
            self.preflight()
        return self._public_key

    def sign_and_record(self, before, after, scope, caller_erased_at=None):
        if self._public_key is None:
            self.preflight()
        r = self._request("POST", "/v1/sign/receipt", {
            "before": before, "after": after, "scope": scope,
            "caller": self.caller, "caller_erased_at": caller_erased_at,
        })
        rd = r["receipt"]
        rcpt = Receipt(before=rd["before"], after=rd["after"], scope=rd["scope"], key_id=rd["key_id"],
                       timestamp=rd["timestamp"], signature=bytes.fromhex(rd["signature"]))
        # never trust a receipt we did not verify against the key we learned at preflight
        if rd["key_id"] != self.key_id or not rcpt.verify(self._public_key):
            raise SignerUnavailable("signer returned a receipt that does not verify against its own key")
        return rcpt, r["entry"]

    def entries(self) -> list[dict]:
        return self._request("GET", "/v1/sign/entries")


class PendingReceipts:
    """Local JSONL of erasures that happened but could not be receipted (remote signer
    failed after the delete). Not a receipt log: a to-do list that `flush_pending` drains."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def add(self, thread_id: str, before: str, after: str, erased_at: str, cause: str) -> None:
        with self._lock:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"thread_id": thread_id, "before": before, "after": after,
                                     "erased_at": erased_at, "cause": cause}) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def drain(self) -> list[dict]:
        with self._lock:
            if not os.path.exists(self.path):
                return []
            with open(self.path, "r", encoding="utf-8") as fh:
                items = [json.loads(l) for l in fh if l.strip()]
            return items

    def rewrite(self, remaining: list[dict]) -> None:
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as fh:
                for it in remaining:
                    fh.write(json.dumps(it) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
