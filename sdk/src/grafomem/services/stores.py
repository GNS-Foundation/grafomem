"""Store management service.

Provides CRUD operations for memory stores.

Usage::

    store = client.stores.create(name="my-agent-memory")
    caps  = client.stores.capabilities(store.id)
    client.stores.flush(store.id)
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from grafomem.types import Store

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class StoresService:
    """Memory store management."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    def create(
        self,
        name: str = "default",
        backend: str = "postgres",
        **kwargs: Any,
    ) -> Store:
        """Create a new memory store.

        Args:
            name: Human-readable store name.
            backend: Backend type (default ``postgres``).

        Returns:
            The created :class:`Store` with its ``id``.
        """
        body: dict[str, Any] = {"name": name, "backend": backend, **kwargs}
        data = self._http.post("/v1/stores", json=body)
        return Store.model_validate(data)

    def list(self) -> list[Store]:
        """List all stores for the current tenant."""
        data = self._http.get("/v1/stores")
        items = data if isinstance(data, list) else data.get("stores", [])
        return [Store.model_validate(s) for s in items]

    def capabilities(self, store_id: str) -> dict[str, Any]:
        """Get declared capabilities for a store.

        Returns:
            Dictionary of capability names to their metadata.
        """
        data = self._http.get(f"/v1/stores/{store_id}/capabilities")
        return data or {}

    def flush(self, store_id: str) -> None:
        """Delete all memories in a store (irreversible)."""
        self._http.post(f"/v1/stores/{store_id}/flush")

    def stats(self, store_id: str) -> dict[str, Any]:
        """Get ingestion statistics for a store."""
        data = self._http.get(f"/v1/stores/{store_id}/stats")
        return data or {}
