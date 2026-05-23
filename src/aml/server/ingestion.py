"""
GRAFOMEM batched ingestion — async queue + batch worker.

The throughput engine: write requests are queued and batched before hitting the
storage layer.  Instead of embedding one memory at a time (batch=1, ~89 w/s),
the worker drains up to `batch_size` items and sends them through the embedder
in a single forward pass via `write_many()`.

Backpressure: the queue is bounded (`max_queue`). If full, `submit()` raises
`QueueFullError` and the HTTP layer returns 503 Service Unavailable.

Latency floor: `flush_interval_ms` ensures a single write doesn't wait more
than N ms for a batch to fill — the worker fires on whichever comes first:
batch_size reached OR timeout expired.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from aml.backends.interface import WriteOptions

logger = logging.getLogger("grafomem.ingestion")


class QueueFullError(Exception):
    """Raised when the ingestion queue has hit its backpressure limit."""


@dataclass
class _PendingWrite:
    """A write awaiting batch commit."""
    content: str
    options: WriteOptions
    future: asyncio.Future  # resolved with the ref (int) once committed
    enqueued_at: float = field(default_factory=time.monotonic)


class IngestionQueue:
    """Async batched ingestion with backpressure.

    Callers ``await queue.submit(content, options)`` and receive the committed
    ref once the batch containing their write is flushed to storage.

    Parameters
    ----------
    backend : SQLiteGMPBackend (or any backend with write_many())
        The storage backend. Must expose ``write_many(items) -> list[int]``.
    batch_size : int
        Maximum items per batch (default 64).
    flush_interval_ms : int
        Maximum milliseconds to wait before flushing an incomplete batch
        (default 50 — the latency floor).
    max_queue : int
        Bounded queue depth. If full, submit() raises QueueFullError.
    """

    def __init__(
        self,
        backend,
        *,
        batch_size: int = 64,
        flush_interval_ms: int = 50,
        max_queue: int = 10_000,
    ) -> None:
        self._backend = backend
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_ms / 1000.0
        self._queue: asyncio.Queue[_PendingWrite] = asyncio.Queue(maxsize=max_queue)
        self._worker_task: asyncio.Task | None = None
        self._running = False

        # Stats
        self.total_submitted: int = 0
        self.total_committed: int = 0
        self.total_batches: int = 0

        # Detect batch write capability
        self._has_write_many = hasattr(backend, "write_many")

    def _write_items(self, items: list[tuple[str, WriteOptions]]) -> list[int]:
        """Write items using write_many() if available, else sequential write()."""
        if self._has_write_many:
            return self._backend.write_many(items)
        return [self._backend.write(content, opts) for content, opts in items]

    async def start(self) -> None:
        """Launch the background batch worker."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker(), name="ingestion-worker")
        logger.info(
            "Ingestion worker started (batch_size=%d, flush_interval=%dms, max_queue=%d)",
            self.batch_size, int(self.flush_interval_s * 1000), self._queue.maxsize,
        )

    async def stop(self) -> None:
        """Drain remaining items and shut down the worker."""
        self._running = False
        if self._worker_task is not None:
            # Drain any remaining items
            await self._flush_remaining()
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        logger.info(
            "Ingestion worker stopped (committed=%d, batches=%d)",
            self.total_committed, self.total_batches,
        )

    async def submit(self, content: str, options: WriteOptions) -> int:
        """Enqueue a write. Returns the ref once the batch commits.

        Raises QueueFullError if the queue is at capacity (backpressure).
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pw = _PendingWrite(content=content, options=options, future=future)
        try:
            self._queue.put_nowait(pw)
        except asyncio.QueueFull:
            raise QueueFullError(
                f"Ingestion queue full ({self._queue.maxsize} items). "
                f"Server is overloaded — try again later."
            )
        self.total_submitted += 1
        return await future

    async def _worker(self) -> None:
        """Background loop: drain queue → batch embed → bulk insert → resolve futures."""
        while self._running:
            batch = await self._drain_batch()
            if batch:
                await self._commit_batch(batch)

    async def _drain_batch(self) -> list[_PendingWrite]:
        """Collect up to batch_size items, waiting at most flush_interval_s."""
        batch: list[_PendingWrite] = []

        # Block until at least one item arrives (or shutdown)
        try:
            first = await asyncio.wait_for(
                self._queue.get(), timeout=self.flush_interval_s
            )
            batch.append(first)
        except asyncio.TimeoutError:
            return batch  # empty — loop back
        except asyncio.CancelledError:
            return batch

        # Greedily drain up to batch_size without blocking
        while len(batch) < self.batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        # If batch is still smaller than batch_size and items might arrive soon,
        # wait briefly for more (up to flush_interval)
        if len(batch) < self.batch_size:
            deadline = time.monotonic() + self.flush_interval_s
            while len(batch) < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=remaining
                    )
                    batch.append(item)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    break

        return batch

    async def _commit_batch(self, batch: list[_PendingWrite]) -> None:
        """Embed + insert a batch via write_many(), then resolve all futures."""
        t0 = time.monotonic()
        items = [(pw.content, pw.options) for pw in batch]

        try:
            # write_many is synchronous (CPU-bound embedding + DB insert).
            # Run in the default executor to avoid blocking the event loop.
            loop = asyncio.get_running_loop()
            refs = await loop.run_in_executor(
                None, self._write_items, items
            )

            # Resolve all futures with their committed refs
            for pw, ref in zip(batch, refs):
                if not pw.future.done():
                    pw.future.set_result(ref)

            elapsed_ms = (time.monotonic() - t0) * 1000
            self.total_committed += len(batch)
            self.total_batches += 1
            logger.debug(
                "Batch committed: %d items in %.1fms (%.0f w/s)",
                len(batch), elapsed_ms,
                len(batch) / (elapsed_ms / 1000) if elapsed_ms > 0 else 0,
            )

        except Exception as exc:
            # On failure, reject all futures in the batch
            for pw in batch:
                if not pw.future.done():
                    pw.future.set_exception(exc)
            logger.error("Batch commit failed (%d items): %s", len(batch), exc)

    async def _flush_remaining(self) -> None:
        """Drain and commit any items left in the queue during shutdown."""
        remaining: list[_PendingWrite] = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if remaining:
            await self._commit_batch(remaining)

    @property
    def pending(self) -> int:
        """Number of items waiting in the queue."""
        return self._queue.qsize()

    def stats(self) -> dict:
        """Return ingestion statistics."""
        return {
            "pending": self.pending,
            "total_submitted": self.total_submitted,
            "total_committed": self.total_committed,
            "total_batches": self.total_batches,
            "avg_batch_size": (
                self.total_committed / self.total_batches
                if self.total_batches > 0 else 0
            ),
        }
