"""GRAFOMEM AutoGen integration.

Provides :class:`GrafomemAutoGenMemory` as a memory backend
for Microsoft AutoGen agents, and :class:`GrafomemGovernanceHook`
for message-level governance.

Usage::

    from grafomem import GrafomemClient
    from grafomem.autogen import GrafomemAutoGenMemory

    client = GrafomemClient(api_key="gfm_...")
    memory = GrafomemAutoGenMemory(client=client, store_id="abc123")
"""

from grafomem.autogen.memory import GrafomemAutoGenMemory
from grafomem.autogen.hooks import GrafomemGovernanceHook

__all__ = ["GrafomemAutoGenMemory", "GrafomemGovernanceHook"]
