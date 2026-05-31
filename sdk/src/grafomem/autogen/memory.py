"""AutoGen memory backend for GRAFOMEM Cloud.

Provides persistent, governed memory for Microsoft AutoGen agents.
Stores and retrieves conversation history through GRAFOMEM Cloud,
enabling audit trails, governance, and GDPR-compliant erasure.

Usage::

    from autogen import AssistantAgent, UserProxyAgent
    from grafomem import GrafomemClient
    from grafomem.autogen import GrafomemAutoGenMemory

    client = GrafomemClient(api_key="gfm_...")
    memory = GrafomemAutoGenMemory(client=client, store_id="ag-mem")

    assistant = AssistantAgent("assistant", llm_config=llm_config)
    # Use memory.add_message() and memory.get_context() in hooks
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from grafomem.client import GrafomemClient

logger = logging.getLogger("grafomem.autogen.memory")


class GrafomemAutoGenMemory:
    """Persistent memory backend for AutoGen agents.

    Stores each message as a GRAFOMEM memory record with metadata
    indicating the sender, role, and session. Retrieves relevant
    context via semantic search for injection into agent prompts.

    Args:
        client: A GrafomemClient instance.
        store_id: The memory store ID to use.
        session_id: Unique session identifier (default "default").
        max_results: Max memories to retrieve for context (default 10).
        source: Source tag for written memories (default "autogen").
    """

    def __init__(
        self,
        client: GrafomemClient,
        store_id: str,
        *,
        session_id: str = "default",
        max_results: int = 10,
        source: str = "autogen",
    ) -> None:
        self._client = client
        self._store_id = store_id
        self._session_id = session_id
        self._max_results = max_results
        self._source = source

    def add_message(
        self,
        content: str,
        *,
        role: str = "user",
        sender: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Store a message in GRAFOMEM."""
        record_meta = {
            "role": role,
            "session": self._session_id,
            "type": "autogen_message",
        }
        if sender:
            record_meta["sender"] = sender
        if meta:
            record_meta.update(meta)

        self._client.memories.write(
            self._store_id,
            content=content,
            source=self._source,
            meta=record_meta,
        )

    def get_context(self, query: str, *, top_k: int | None = None) -> list[dict[str, Any]]:
        """Retrieve relevant memories for context injection."""
        results = self._client.memories.retrieve(
            self._store_id,
            query=query,
            top_k=top_k or self._max_results,
        )
        return [
            {
                "content": r.content,
                "role": getattr(r, 'meta', {}).get("role", "user"),
                "score": getattr(r, 'score', 1.0),
                "metadata": getattr(r, 'meta', {}),
            }
            for r in results
        ]

    def get_messages(
        self,
        *,
        sender: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve stored messages, optionally filtered by sender."""
        results = self._client.memories.retrieve(
            self._store_id,
            query=f"session:{self._session_id}",
            top_k=limit or self._max_results,
        )
        messages = []
        for r in results:
            meta = getattr(r, 'meta', {})
            if meta.get("session") != self._session_id:
                continue
            if sender and meta.get("sender") != sender:
                continue
            messages.append({
                "content": r.content,
                "role": meta.get("role", "user"),
                "sender": meta.get("sender"),
            })
        return messages

    def clear(self) -> None:
        """Clear all memories from the store."""
        self._client.stores.flush(self._store_id)
