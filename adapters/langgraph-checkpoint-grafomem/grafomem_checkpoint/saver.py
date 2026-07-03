from typing import Any, AsyncIterator, Dict, Iterator, Mapping, Sequence, Collection
from datetime import datetime, timezone
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

class GrafomemCheckpointSaver(BaseCheckpointSaver):
    """
    Layer 2: LangGraph Checkpoint Saver Decorator for Lethe.
    Delegates all storage operations to an inner saver.
    Emits out-of-band erasure Receipts on delete_thread.
    """
    
    def __init__(self, inner: BaseCheckpointSaver):
        super().__init__(serde=inner.serde)
        self.inner = inner
        if not isinstance(self.inner.serde, GrafomemSerializer):
            raise ValueError("Inner saver must use GrafomemSerializer as its serde")
        self._receipts: Dict[str, Receipt] = {}
        
    def last_receipt(self, thread_id: str) -> Receipt | None:
        return self._receipts.get(thread_id)
        
    def _create_receipt(self, thread_id: str) -> Receipt:
        timestamp = datetime.now(timezone.utc).isoformat()
        
        payload = f"thread_data|erased|{thread_id}|{self.inner.serde.key_id}|{timestamp}".encode('utf-8')
        signature = self.inner.serde.private_key.sign(payload)
        
        return Receipt(
            before="thread_data",
            after="erased",
            scope=thread_id,
            key_id=self.inner.serde.key_id,
            timestamp=timestamp,
            signature=signature
        )
        
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
        self.inner.delete_thread(thread_id)
        # Emit out-of-band erasure receipt
        rcpt = self._create_receipt(thread_id)
        self._receipts[thread_id] = rcpt

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
        await self.inner.adelete_thread(thread_id)
        rcpt = self._create_receipt(thread_id)
        self._receipts[thread_id] = rcpt
