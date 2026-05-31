"""CrewAI storage backend for GRAFOMEM Cloud.

Implements CrewAI's storage interface so GRAFOMEM can be used
as the memory backend for any CrewAI Crew.

Usage::

    from crewai import Crew, Agent, Task
    from grafomem import GrafomemClient
    from grafomem.crewai import GrafomemCrewStorage

    client = GrafomemClient(api_key="gfm_...")
    storage = GrafomemCrewStorage(client=client, store_id="crew-mem")

    # CrewAI will use GRAFOMEM for agent memory
    crew = Crew(
        agents=[...],
        tasks=[...],
        memory=True,
    )
"""
from __future__ import annotations

import json
import time
from typing import Any

try:
    from crewai.memory.storage.interface import Storage
except ImportError:
    raise ImportError(
        "crewai is required for the CrewAI adapter. "
        "Install it with: pip install 'grafomem[crewai]'"
    )

from grafomem.client import GrafomemClient


class GrafomemCrewStorage(Storage):
    """CrewAI storage backed by GRAFOMEM Cloud.

    Drop-in replacement for CrewAI's built-in RAGStorage or LTMStorage.
    All memory operations are persisted to the GRAFOMEM memory store,
    enabling governance, audit trails, and erasure compliance.

    Args:
        client: A GrafomemClient instance.
        store_id: The memory store ID to use.
        source: Source tag for written memories (default "crewai").
    """

    def __init__(
        self,
        client: GrafomemClient,
        store_id: str,
        *,
        source: str = "crewai",
    ) -> None:
        self._client = client
        self._store_id = store_id
        self._source = source

    def save(self, value: Any, metadata: dict[str, Any] | None = None) -> None:
        """Save a memory to the GRAFOMEM store."""
        content = value if isinstance(value, str) else json.dumps(value)
        meta = metadata or {}
        meta["type"] = "crew_memory"
        self._client.memories.write(
            self._store_id,
            content=content,
            source=self._source,
            meta=meta,
        )

    def search(self, query: str, limit: int = 3, score_threshold: float = 0.35) -> list[dict]:
        """Search memories by semantic similarity."""
        results = self._client.memories.retrieve(
            self._store_id,
            query=query,
            top_k=limit,
        )
        return [
            {
                "context": r.content,
                "score": getattr(r, 'score', 1.0),
                "metadata": getattr(r, 'meta', {}),
            }
            for r in results
        ]

    def reset(self) -> None:
        """Clear all memories from the store."""
        self._client.stores.flush(self._store_id)
