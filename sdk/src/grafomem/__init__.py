"""GRAFOMEM Python SDK — governed agent memory platform.

Usage (sync)::

    from grafomem import GrafomemClient

    client = GrafomemClient(api_key="gfm_...", base_url="https://cloud.grafomem.com")
    store = client.stores.create(name="my-agent-memory")
    client.memories.write(store.id, text="User prefers dark mode")
    results = client.memories.retrieve(store.id, query="user preferences")

Usage (async — v2)::

    from grafomem import GrafomemAsyncClient

    async with GrafomemAsyncClient(api_key="gfm_...") as client:
        store = await client.stores.create(name="demo")
        await client.memories.write(store["store_id"], text="Hello")
"""

from grafomem.client import GrafomemClient
from grafomem.async_client import GrafomemAsyncClient
from grafomem.pagination import Paginator, AsyncPaginator
from grafomem.webhooks import verify_signature, compute_signature
from grafomem.errors import (
    GrafomemError,
    APIError,
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)

__all__ = [
    # Clients
    "GrafomemClient",
    "GrafomemAsyncClient",
    # Pagination
    "Paginator",
    "AsyncPaginator",
    # Webhooks
    "verify_signature",
    "compute_signature",
    # Errors
    "GrafomemError",
    "APIError",
    "AuthenticationError",
    "NotFoundError",
    "RateLimitError",
    "ValidationError",
]

__version__ = "0.2.0"
