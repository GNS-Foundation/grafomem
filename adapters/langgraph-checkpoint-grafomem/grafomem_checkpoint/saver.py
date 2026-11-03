from typing import Any, AsyncIterator, Dict, Iterator, Mapping, Sequence, Collection
from datetime import datetime, timezone
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

# Sentinel used in a receipt's `before` field when a delete_thread targets a thread that
# had no checkpoints — the receipt still records that an erase was requested, but there is
# no content to bind.
EMPTY_THREAD_DIGEST = "sha256:empty"


class GrafomemCheckpointSaver(BaseCheckpointSaver):
    """
    Layer 2: LangGraph Checkpoint Saver Decorator for Lethe.
    Delegates all storage operations to an inner saver.

    On delete_thread it emits a signed erasure Receipt whose `before` field binds a
    SHA-256 digest of the thread's entire checkpoint history *as it stood immediately
    before deletion* — so the receipt commits to the specific data that was erased, not
    just to the fact that an erase happened. Each receipt is also appended to an
    append-only, hash-chained ReceiptLog, so an auditor can distinguish a real deletion
    from a delete_thread that was never called.

    What the receipt does and does not prove (stated plainly): it is tamper-evidence and
    non-repudiation for the erase *transition*, bound to a key and timestamp. It is not a
    claim of media-level unrecoverability — the inner store's WAL, dead tuples, replicas,
    snapshots and backups are outside this wrapper's control.
    """

    def __init__(self, inner: BaseCheckpointSaver, receipt_log: ReceiptLog | None = None):
        super().__init__(serde=inner.serde)
        self.inner = inner
        if not isinstance(self.inner.serde, GrafomemSerializer):
            raise ValueError("Inner saver must use GrafomemSerializer as its serde")
        self._receipts: Dict[str, Receipt] = {}
        # Durable, append-only audit log of erasures. Defaults to a JSONL file; the path
        # may be set with GRAFOMEM_RECEIPT_LOG (default: ./grafomem_receipts.jsonl).
        if receipt_log is None:
            receipt_log = FileReceiptLog(
                os.environ.get("GRAFOMEM_RECEIPT_LOG", "grafomem_receipts.jsonl")
            )
        self._log = receipt_log

    def last_receipt(self, thread_id: str) -> Receipt | None:
        return self._receipts.get(thread_id)

    def audit_log(self) -> list[dict]:
        """Return the append-only erasure log (all entries, in append order)."""
        return self._log.entries()

    # --- Erasure receipt construction ---

    def _thread_digest(self, tuples: list[CheckpointTuple]) -> str:
        """Deterministic SHA-256 over a thread's checkpoint history.

        Each checkpoint contributes `"<checkpoint_id>:<content_hash>"`; entries are sorted
        by checkpoint id (UUID7 — lexicographic order is chronological) so the digest does
        not depend on listing order. An empty thread yields EMPTY_THREAD_DIGEST.
        """
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

    def _create_receipt(self, thread_id: str, before_digest: str) -> Receipt:
        timestamp = datetime.now(timezone.utc).isoformat()
        before = before_digest
        after = "erased"
        key_id = self.inner.serde.key_id
        # Payload matches Receipt.verify(): before|after|scope|key_id|timestamp — so the
        # signature commits to `before` (the content digest).
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

    def _record(self, thread_id: str, before_digest: str) -> Receipt:
        rcpt = self._create_receipt(thread_id, before_digest)
        self._receipts[thread_id] = rcpt
        self._log.append(rcpt)
        return rcpt

    # --- Delegated Methods ---

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.inner.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        return self.inner.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        content_hash = self.inner.serde.compute_content_hash(checkpoint)

        # Inject content_hash into metadata to retain UUID7 keying for LangGraph
        new_metadata = dict(metadata) if metadata else {}
        new_metadata["grafomem_content_hash"] = content_hash

        return self.inner.put(config, checkpoint, new_metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        return self.inner.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        # Bind the pre-image BEFORE the data is gone, then delete, then record.
        tuples = list(self.inner.list({"configurable": {"thread_id": thread_id}}))
        before_digest = self._thread_digest(tuples)
        self.inner.delete_thread(thread_id)
        self._record(thread_id, before_digest)

    # --- Async Delegated Methods ---
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await self.inner.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
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
        return await self.inner.aput(config, checkpoint, new_metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        return await self.inner.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        tuples = [
            t async for t in self.inner.alist({"configurable": {"thread_id": thread_id}})
        ]
        before_digest = self._thread_digest(tuples)
        await self.inner.adelete_thread(thread_id)
        self._record(thread_id, before_digest)
