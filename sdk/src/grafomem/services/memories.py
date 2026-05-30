"""Memory read/write service.

Provides write, retrieve, delete, supersede, and audit operations on
individual memory records within a store.

Usage::

    client.memories.write(store_id, content="User prefers dark mode")
    results = client.memories.retrieve(store_id, query="preferences")
    client.memories.delete(store_id, ref=results[0].ref)
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from grafomem.types import MemoryRecord, WriteResult

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class MemoriesService:
    """Memory record operations."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    def write(
        self,
        store_id: str,
        content: str,
        *,
        source: str = "",
        meta: dict[str, Any] | None = None,
        text: str = "",
        **kwargs: Any,
    ) -> WriteResult:
        """Write a single memory record.

        Args:
            store_id: Target store ID.
            content: The memory text content.
            source: Optional source identifier (stored in metadata).
            meta: Optional metadata dictionary.
            text: Alias for ``content`` (for convenience).

        Returns:
            A :class:`WriteResult` with the assigned ``ref``.
        """
        actual_content = content or text
        metadata: dict[str, Any] = dict(meta or {})
        if source:
            metadata["source"] = source

        body: dict[str, Any] = {
            "content": actual_content,
            "options": {"metadata": metadata},
            **kwargs,
        }
        data = self._http.post(f"/v1/stores/{store_id}/write", json=body)
        return WriteResult.model_validate(data)

    def write_batch(
        self,
        store_id: str,
        items: list[str] | list[dict[str, Any]],
    ) -> list[int]:
        """Write multiple memory records in a single request.

        Args:
            store_id: Target store ID.
            items: List of content strings or dicts with ``content`` key.

        Returns:
            List of assigned refs.
        """
        api_items = []
        for item in items:
            if isinstance(item, str):
                api_items.append({"content": item, "options": {}})
            else:
                api_items.append({
                    "content": item.get("content", item.get("text", "")),
                    "options": item.get("options", {}),
                })

        data = self._http.post(
            f"/v1/stores/{store_id}/write_batch",
            json={"items": api_items},
        )
        return data.get("refs", [])

    def retrieve(
        self,
        store_id: str,
        query: str,
        *,
        top_k: int = 5,
        **kwargs: Any,
    ) -> list[MemoryRecord]:
        """Retrieve memories by semantic similarity.

        Args:
            store_id: Source store ID.
            query: The search query text.
            top_k: Maximum number of results (default 5).

        Returns:
            List of :class:`MemoryRecord` sorted by relevance.
        """
        body: dict[str, Any] = {
            "query": query,
            "options": {"top_k": top_k},
        }
        data = self._http.post(f"/v1/stores/{store_id}/retrieve", json=body)
        items = data.get("memories", []) if isinstance(data, dict) else data
        return [MemoryRecord.model_validate(r) for r in items]

    def delete(self, store_id: str, ref: int | str) -> bool:
        """Delete a memory record by reference.

        Args:
            store_id: Store containing the record.
            ref: The record's ``ref`` identifier.

        Returns:
            True if the record was deleted.
        """
        data = self._http.post(
            f"/v1/stores/{store_id}/delete",
            json={"ref": ref},
        )
        return data.get("deleted", False) if isinstance(data, dict) else False

    def supersede(
        self,
        store_id: str,
        old_ref: int | str,
        content: str,
        **kwargs: Any,
    ) -> WriteResult:
        """Replace a memory record with an updated version.

        The old record is marked as superseded and a new record is
        created with a back-link.

        Args:
            store_id: Store containing the record.
            old_ref: The ``ref`` of the record to supersede.
            content: The replacement text.

        Returns:
            A :class:`WriteResult` for the new record.
        """
        body: dict[str, Any] = {
            "old_ref": old_ref,
            "content": content,
            "options": kwargs.get("options", {}),
        }
        data = self._http.post(f"/v1/stores/{store_id}/supersede", json=body)
        return WriteResult.model_validate(data)

    def audit(self, store_id: str) -> list[MemoryRecord]:
        """Get the full audit log for a store.

        Returns:
            List of :class:`MemoryRecord` showing all versions.
        """
        data = self._http.get(f"/v1/stores/{store_id}/audit")
        items = data.get("memories", []) if isinstance(data, dict) else data
        return [MemoryRecord.model_validate(r) for r in items]
