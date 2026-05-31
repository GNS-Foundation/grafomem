"""GRAFOMEM CrewAI integration.

Provides :class:`GrafomemCrewStorage` as a drop-in memory backend
for CrewAI crews, and :class:`GrafomemGovernanceCallback` for
pre/post governance hooks on task execution.

Usage::

    from grafomem import GrafomemClient
    from grafomem.crewai import GrafomemCrewStorage

    client = GrafomemClient(api_key="gfm_...")
    storage = GrafomemCrewStorage(client=client, store_id="abc123")
"""

from grafomem.crewai.storage import GrafomemCrewStorage
from grafomem.crewai.callbacks import GrafomemGovernanceCallback

__all__ = ["GrafomemCrewStorage", "GrafomemGovernanceCallback"]
