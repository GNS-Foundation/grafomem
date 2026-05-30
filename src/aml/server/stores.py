"""
GRAFOMEM store lifecycle manager — create / route / destroy backend instances.

Thread-safe registry of named stores. Each store is a fully independent
MemoryBackend instance with its own ingestion queue. The server routes
requests to the correct store by store_id extracted from the URL path.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("grafomem.stores")


@dataclass
class StoreEntry:
    """A registered store with its backend and optional ingestion queue."""
    store_id: str
    backend: Any  # MemoryBackend
    queue: Any | None = None  # IngestionQueue, if batched mode is enabled
    created_at: str = ""
    owner_tenant_id: str | None = None  # tenant that created this store


class StoreManager:
    """Thread-safe store registry with lazy initialization.

    Parameters
    ----------
    backend_factory : callable
        A no-arg callable that returns a fresh MemoryBackend instance.
    """

    def __init__(self, backend_factory) -> None:
        self._factory = backend_factory
        self._stores: dict[str, StoreEntry] = {}
        self._lock = threading.Lock()

    def create(self, tenant_id: str | None = None) -> str:
        """Create a new store and return its ID."""
        from datetime import datetime, timezone
        store_id = uuid.uuid4().hex[:12]
        backend = self._factory()
        entry = StoreEntry(
            store_id=store_id,
            backend=backend,
            created_at=datetime.now(timezone.utc).isoformat(),
            owner_tenant_id=tenant_id,
        )
        with self._lock:
            self._stores[store_id] = entry
        logger.info("Store created: %s (owner=%s)", store_id, tenant_id or 'none')
        return store_id

    def get(self, store_id: str) -> StoreEntry | None:
        """Get a store entry by ID. Returns None if not found."""
        with self._lock:
            return self._stores.get(store_id)

    def get_or_404(self, store_id: str) -> StoreEntry:
        """Get a store entry by ID. Raises KeyError if not found."""
        entry = self.get(store_id)
        if entry is None:
            raise KeyError(f"Store '{store_id}' not found")
        return entry

    def delete(self, store_id: str) -> bool:
        """Delete a store. Returns True if it existed."""
        with self._lock:
            entry = self._stores.pop(store_id, None)
        if entry is not None:
            if hasattr(entry.backend, "close"):
                try:
                    entry.backend.close()
                except Exception:
                    pass
            logger.info("Store deleted: %s", store_id)
            return True
        return False

    def list_stores(self) -> list[dict]:
        """List all registered stores."""
        with self._lock:
            return [
                {"store_id": e.store_id, "created_at": e.created_at}
                for e in self._stores.values()
            ]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._stores)

    def get_default(self) -> StoreEntry:
        """Get or create the default store (for single-store mode)."""
        with self._lock:
            if "__default__" in self._stores:
                return self._stores["__default__"]
        # Create it outside the lock (factory may be slow)
        from datetime import datetime, timezone
        backend = self._factory()
        entry = StoreEntry(
            store_id="__default__",
            backend=backend,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            if "__default__" not in self._stores:
                self._stores["__default__"] = entry
            return self._stores["__default__"]
