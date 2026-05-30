"""GRAFOMEM Python SDK — governed agent memory platform.

Usage::

    from grafomem import GrafomemClient

    client = GrafomemClient(api_key="gfm_...", base_url="https://cloud.grafomem.com")
    store = client.stores.create(name="my-agent-memory")
    client.memories.write(store.id, text="User prefers dark mode")
    results = client.memories.retrieve(store.id, query="user preferences")
"""

from grafomem.client import GrafomemClient
from grafomem.errors import (
    GrafomemError,
    APIError,
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)

__all__ = [
    "GrafomemClient",
    "GrafomemError",
    "APIError",
    "AuthenticationError",
    "NotFoundError",
    "RateLimitError",
    "ValidationError",
]

__version__ = "0.1.0"
