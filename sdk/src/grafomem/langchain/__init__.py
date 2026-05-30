"""GRAFOMEM LangChain integration.

Provides :class:`GrafomemMemory` (for chains) and
:class:`GrafomemChatMessageHistory` (for LangGraph / LCEL).

Usage::

    from grafomem import GrafomemClient
    from grafomem.langchain import GrafomemMemory

    client = GrafomemClient(api_key="gfm_...")
    memory = GrafomemMemory(client=client, store_id="abc123")
"""

from grafomem.langchain.memory import GrafomemMemory
from grafomem.langchain.history import GrafomemChatMessageHistory

__all__ = ["GrafomemMemory", "GrafomemChatMessageHistory"]
