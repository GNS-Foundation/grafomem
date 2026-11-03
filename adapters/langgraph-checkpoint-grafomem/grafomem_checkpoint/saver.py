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

    def __init__(self, inner: BaseCheckpointSaver, receipt_log: ReceiptLog | None = None):
        super().__init__(serde=inner.serde)
        self.inner = inner
        if not isinstance(self.inner.serde, GrafomemSerializer):
            raise ValueError("Inner saver must use GrafomemSerializer as its serde")
        self._receipts: Dict[str, Receipt] = {}
        if receipt_log is None:
            receipt_log = FileReceiptLog(
                os.environ.get("GRAFOMEM_RECEIPT_LOG", "grafomem_receipts.jsonl")
            )
        self._log = receipt_log

    @property
    def _key_manager(self):
        return getattr(self.inner.serde, "key_manager", None)

    def last_receipt(self, thread_id: str) -> Receipt | None:
        return self._receipts.get(thread_id)

    def audit_log(self) -> list[dict]:
        """Return the append-only erasure log (all entries, in append order)."""
        return self._log.entries()

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

    def _create_receipt(self, thread_id: str, before_digest: str, after: str = "erased") -> Receipt:
        timestamp = datetime.now(timezone.utc).isoformat()
        before = before_digest
        key_id = self.inner.serde.key_id
        payload = f"{before}|{after}|{thread_id}|{key_id}|{timestamp}".encode("utf-8")
        signature = self.inner.serde.private_key.sign(payload)
        return Receipt(
            before=before,
            after=after,
            scope=thread_id,
            key_id=key_id,
            timestamp=timestamp,
            signature=signature,
        )

    def _record(self, thread_id: str, before_digest: str, after: str = "erased") -> Receipt:
        rcpt = self._create_receipt(thread_id, before_digest, after)
        self._receipts[thread_id] = rcpt
        self._log.append(rcpt)
        return rcpt

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
        # Bind the pre-image BEFORE the data is gone (needs the key alive to read it), then
        # destroy the key, then record.
        with _thread_ctx(thread_id):
            tuples = list(self.inner.list({"configurable": {"thread_id": thread_id}}))
            before_digest = self._thread_digest(tuples)
        self.inner.delete_thread(thread_id)
        after = "erased"
        if self._key_manager is not None:
            fp = self._key_manager.destroy_key(thread_id)
            after = f"key_destroyed:{fp}"
        self._record(thread_id, before_digest, after)

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
        with _thread_ctx(thread_id):
            tuples = [
                t async for t in self.inner.alist({"configurable": {"thread_id": thread_id}})
            ]
            before_digest = self._thread_digest(tuples)
        await self.inner.adelete_thread(thread_id)
        after = "erased"
        if self._key_manager is not None:
            fp = self._key_manager.destroy_key(thread_id)
            after = f"key_destroyed:{fp}"
        self._record(thread_id, before_digest, after)
