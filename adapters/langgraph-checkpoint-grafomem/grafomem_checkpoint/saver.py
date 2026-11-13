from typing import Any, AsyncIterator, Dict, Iterator, Mapping, Sequence, Collection
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import os
import copy
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
    RunnableConfig,
)
from grafomem.runtime import Receipt
from .serializer import GrafomemSerializer
from .receipt_log import ReceiptLog, FileReceiptLog
from .thread_keys import current_thread_id
from .signer import (
    ReceiptSigner, LocalReceiptSigner, RemoteReceiptSigner,
    PendingReceipts, ReceiptPending, SignerUnavailable,
)

# Sentinel used in a receipt's `before` field when a delete_thread targets a thread that
# had no checkpoints — the receipt still records that an erase was requested, but there is
# no content to bind.
EMPTY_THREAD_DIGEST = "sha256:empty"


def _thread_id_of(config: RunnableConfig | None) -> str | None:
    if not config:
        return None
    return (config.get("configurable") or {}).get("thread_id")


@contextmanager
def _thread_ctx(thread_id: str | None):
    token = current_thread_id.set(thread_id)
    try:
        yield
    finally:
        current_thread_id.reset(token)


class GrafomemCheckpointSaver(BaseCheckpointSaver):
    """
    Layer 2: LangGraph Checkpoint Saver Decorator for Lethe.
    Delegates all storage operations to an inner saver.

    On delete_thread it emits a signed erasure Receipt whose `before` field binds a SHA-256
    digest of the thread's entire checkpoint history before deletion, and appends it to an
    append-only, hash-chained ReceiptLog.

    If the inner serde is configured with a per-thread key_manager (crypto-erasure, opt-in),
    delete_thread also destroys the thread's encryption key and the receipt's `after` field
    carries `key_destroyed:<fingerprint>`, committing to that destruction — so residual
    ciphertext outside this wrapper (WAL, dead tuples, replicas, snapshots, backups) becomes
    undecryptable. Without a key_manager, behavior is unchanged (`after="erased"`).
    """

    def __init__(
        self,
        inner: BaseCheckpointSaver,
        receipt_log: ReceiptLog | None = None,
        *,
        receipt_signer: ReceiptSigner | None = None,
        allow_unreceipted: bool = False,
        pending_path: str | None = None,
    ):
        super().__init__(serde=inner.serde)
        self.inner = inner
        if not isinstance(self.inner.serde, GrafomemSerializer):
            raise ValueError("Inner saver must use GrafomemSerializer as its serde")
        self._receipts: Dict[str, Receipt] = {}

        # ADR-0020 (C4): who signs receipts and who may write the log.
        #   receipt_signer given      → that signer (RemoteReceiptSigner: this process holds no
        #                               receipt key and no log handle).
        #   else                      → LocalReceiptSigner over the serializer's key and the
        #                               receipt_log (pre-1.3.0 behaviour, now named).
        if receipt_signer is not None and receipt_log is not None:
            raise ValueError("pass either receipt_signer or receipt_log, not both: a remote signer owns the log")
        if receipt_signer is None:
            if receipt_log is None:
                receipt_log = FileReceiptLog(
                    os.environ.get("GRAFOMEM_RECEIPT_LOG", "grafomem_receipts.jsonl")
                )
            receipt_signer = LocalReceiptSigner(self.inner.serde.private_key, self.inner.serde.key_id, receipt_log)
        self._signer = receipt_signer
        self._allow_unreceipted = allow_unreceipted
        self._pending = PendingReceipts(
            pending_path or os.environ.get("GRAFOMEM_RECEIPT_PENDING", "grafomem_receipts_pending.jsonl")
        )
        # a remote signer's key is learned at preflight; make it verifiable from here
        if getattr(receipt_signer, "kind", "local") == "remote":
            try:
                receipt_signer.preflight()
                self.inner.serde.trusted_keys[receipt_signer.key_id] = receipt_signer.public_key()
            except SignerUnavailable:
                pass  # checked again, and refused, at delete time (fail closed)

    @property
    def receipt_signer(self) -> ReceiptSigner:
        return self._signer

    @property
    def _key_manager(self):
        return getattr(self.inner.serde, "key_manager", None)

    def last_receipt(self, thread_id: str) -> Receipt | None:
        return self._receipts.get(thread_id)

    def audit_log(self) -> list[dict]:
        """Entries of the receipt log — read through the signer (a remote signer serves
        them read-only; this process never holds the log's write handle)."""
        """Return the append-only erasure log (all entries, in append order)."""
        return self._signer.entries()

    # --- Erasure receipt construction ---

    def _thread_digest(self, tuples: list[CheckpointTuple]) -> str:
        parts: list[str] = []
        for t in tuples:
            cid = t.checkpoint.get("id", "")
            chash = (t.metadata or {}).get("grafomem_content_hash")
            if not chash:
                chash = self.inner.serde.compute_content_hash(t.checkpoint)
            parts.append(f"{cid}:{chash}")
        if not parts:
            return EMPTY_THREAD_DIGEST
        parts.sort()
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _record(self, thread_id: str, before_digest: str, after: str = "erased",
                erased_at: str | None = None) -> Receipt | None:
        """Sign and append through the signer. The erasure has ALREADY happened when this
        runs; if the signer fails now the thread is erased and unreceipted — a pending
        marker is written and ReceiptPending raised (or None returned with
        allow_unreceipted=True). Never a locally-forged stand-in receipt."""
        erased_at = erased_at or datetime.now(timezone.utc).isoformat()
        try:
            rcpt, _entry = self._signer.sign_and_record(before_digest, after, thread_id, caller_erased_at=erased_at)
        except SignerUnavailable as e:
            self._pending.add(thread_id, before_digest, after, erased_at, str(e))
            if self._allow_unreceipted:
                return None
            raise ReceiptPending(thread_id, self._pending.path, e) from e
        if rcpt.key_id not in self.inner.serde.trusted_keys:
            self.inner.serde.trusted_keys[rcpt.key_id] = self._signer.public_key()
        self._receipts[thread_id] = rcpt
        return rcpt

    def flush_pending(self) -> int:
        """Retry every pending (erased-but-unreceipted) thread through the signer. Returns
        how many were receipted; the rest stay pending. Entries recorded this way carry a
        `caller_erased_at` earlier than their `recorded_at` — the gap is visible."""
        items = self._pending.drain()
        if not items:
            return 0
        self._signer.preflight()
        remaining, done = [], 0
        for it in items:
            try:
                rcpt, _ = self._signer.sign_and_record(it["before"], it["after"], it["thread_id"],
                                                       caller_erased_at=it["erased_at"])
                self._receipts[it["thread_id"]] = rcpt
                done += 1
            except SignerUnavailable:
                remaining.append(it)
        self._pending.rewrite(remaining)
        return done

    def pending(self) -> list[dict]:
        return self._pending.drain()

    # --- Delegated Methods ---

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with _thread_ctx(_thread_id_of(config)):
            return self.inner.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        # Keep the thread-id in scope for the whole lazy iteration (loads_typed runs per item).
        with _thread_ctx(_thread_id_of(config)):
            yield from self.inner.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        content_hash = self.inner.serde.compute_content_hash(checkpoint)
        new_metadata = dict(metadata) if metadata else {}
        new_metadata["grafomem_content_hash"] = content_hash
        with _thread_ctx(_thread_id_of(config)):
            return self.inner.put(config, checkpoint, new_metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        with _thread_ctx(_thread_id_of(config)):
            return self.inner.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        # Fail closed (ADR-0020 F1): no signer, no erasure. Checked BEFORE anything is gone.
        self._signer.preflight()
        # Bind the pre-image BEFORE the data is gone (needs the key alive to read it), then
        # destroy the key, then record.
        with _thread_ctx(thread_id):
            tuples = list(self.inner.list({"configurable": {"thread_id": thread_id}}))
            before_digest = self._thread_digest(tuples)
        self.inner.delete_thread(thread_id)
        erased_at = datetime.now(timezone.utc).isoformat()
        after = "erased"
        if self._key_manager is not None:
            fp = self._key_manager.destroy_key(thread_id)
            after = f"key_destroyed:{fp}"
        self._record(thread_id, before_digest, after, erased_at=erased_at)

    # --- Async Delegated Methods ---
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with _thread_ctx(_thread_id_of(config)):
            return await self.inner.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        with _thread_ctx(_thread_id_of(config)):
            async for t in self.inner.alist(config, filter=filter, before=before, limit=limit):
                yield t

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        content_hash = self.inner.serde.compute_content_hash(checkpoint)
        new_metadata = dict(metadata) if metadata else {}
        new_metadata["grafomem_content_hash"] = content_hash
        with _thread_ctx(_thread_id_of(config)):
            return await self.inner.aput(config, checkpoint, new_metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        with _thread_ctx(_thread_id_of(config)):
            return await self.inner.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self._signer.preflight()  # fail closed, before anything is gone
        with _thread_ctx(thread_id):
            tuples = [
                t async for t in self.inner.alist({"configurable": {"thread_id": thread_id}})
            ]
            before_digest = self._thread_digest(tuples)
        await self.inner.adelete_thread(thread_id)
        erased_at = datetime.now(timezone.utc).isoformat()
        after = "erased"
        if self._key_manager is not None:
            fp = self._key_manager.destroy_key(thread_id)
            after = f"key_destroyed:{fp}"
        self._record(thread_id, before_digest, after, erased_at=erased_at)
