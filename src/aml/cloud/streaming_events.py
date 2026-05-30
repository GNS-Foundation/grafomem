"""
GRAFOMEM Real-Time Streaming — event emitter for SSE workflow execution.

Provides a thread-safe event emitter that bridges the synchronous orchestrator
with the async SSE endpoint.  During workflow execution, ``execute_step()``
calls ``emitter.emit(event_type, data)`` at each governed stage; the SSE
endpoint consumes events via ``emitter.events()`` (an async generator).

Event types:
    workflow.started       — workflow execution begins
    step.started           — a single agent step starts
    step.governance_pass   — governance gate allowed
    step.governance_deny   — governance gate denied / escalated
    step.memory_retrieve   — memory retrieval completed
    step.llm_start         — LLM inference begins
    step.llm_complete      — LLM inference returns
    step.tool_call         — a tool was executed
    step.complete          — step fully done (persisted + receipt issued)
    workflow.complete      — all steps done
    workflow.error         — unhandled error
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

logger = logging.getLogger("grafomem.cloud.streaming")

# Sentinel for stream closure
_STREAM_END = object()


@dataclass(slots=True)
class StreamEvent:
    """A single SSE event emitted during workflow execution."""

    event: str
    """Event type (e.g. ``step.governance_pass``)."""

    data: dict[str, Any]
    """JSON-serializable event payload."""

    timestamp: str
    """ISO 8601 timestamp."""

    workflow_id: str
    """The workflow being executed."""

    step_index: int | None = None
    """Current step index (0-based), if applicable."""

    agent_name: str | None = None
    """Agent name for step-level events."""

    def to_sse(self) -> str:
        """Format as an SSE wire message."""
        payload = {
            **self.data,
            "timestamp": self.timestamp,
            "workflow_id": self.workflow_id,
        }
        if self.step_index is not None:
            payload["step_index"] = self.step_index
        if self.agent_name:
            payload["agent_name"] = self.agent_name
        return json.dumps(payload, default=str)


class StreamEmitter:
    """Thread-safe event emitter bridging sync orchestrator → async SSE.

    The orchestrator runs in a background thread (sync).  ``emit()`` is
    called from that thread and pushes events into an asyncio.Queue via
    ``call_soon_threadsafe``.  The SSE endpoint consumes events via the
    ``events()`` async generator.

    Usage (server side)::

        emitter = StreamEmitter(loop=asyncio.get_event_loop())
        # In background thread:
        emitter.emit("step.complete", {"tokens": 42}, workflow_id="abc")
        emitter.close()
        # In async endpoint:
        async for event in emitter.events():
            yield event.to_sse()
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._workflow_id: str = ""
        self._start_time: float = time.monotonic()

    def set_workflow(self, workflow_id: str) -> None:
        """Set the workflow ID for all subsequent events."""
        self._workflow_id = workflow_id
        self._start_time = time.monotonic()

    def emit(
        self,
        event: str,
        data: dict[str, Any],
        *,
        step_index: int | None = None,
        agent_name: str | None = None,
    ) -> None:
        """Emit an event (thread-safe — called from sync orchestrator thread).

        Args:
            event: Event type string (e.g. ``"step.governance_pass"``).
            data: JSON-serializable payload.
            step_index: Current step index (0-based).
            agent_name: Agent name for step-level events.
        """
        if self._closed:
            return

        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
        data_with_elapsed = {**data, "elapsed_ms": elapsed_ms}

        evt = StreamEvent(
            event=event,
            data=data_with_elapsed,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            workflow_id=self._workflow_id,
            step_index=step_index,
            agent_name=agent_name,
        )

        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, evt)
        except RuntimeError:
            # Event loop closed — ignore
            logger.debug("Event loop closed, dropping event: %s", event)

    def close(self) -> None:
        """Signal end of stream."""
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, _STREAM_END,
            )
        except RuntimeError:
            pass

    async def events(self) -> AsyncGenerator[StreamEvent, None]:
        """Async generator consuming emitted events.

        Yields :class:`StreamEvent` instances until the stream is closed.
        """
        while True:
            item = await self._queue.get()
            if item is _STREAM_END:
                return
            yield item
