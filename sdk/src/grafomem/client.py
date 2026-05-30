"""GRAFOMEM Cloud client.

The :class:`GrafomemClient` is the main entry point for the SDK.
It provides access to all API services through typed properties.

Usage::

    from grafomem import GrafomemClient

    client = GrafomemClient(
        api_key="gfm_...",
        base_url="https://cloud.grafomem.com",
    )

    # Access services
    store  = client.stores.create(name="my-agent-memory")
    client.memories.write(store.id, text="User prefers dark mode")
    result = client.governance.evaluate("inference", {"model_id": "gpt-4o"})

    # Context manager for clean shutdown
    with GrafomemClient(api_key="gfm_...") as client:
        ...
"""

from __future__ import annotations

from typing import Any

from grafomem._http import HTTPTransport
from grafomem.services.stores import StoresService
from grafomem.services.memories import MemoriesService
from grafomem.services.governance import GovernanceService
from grafomem.services.orchestrator import OrchestratorService
from grafomem.services.decisions import DecisionsService
from grafomem.services.erasure import ErasureService
from grafomem.services.landing import LandingService as _Landing
from grafomem.services.artifacts import ArtifactsService
from grafomem.services.reports import ReportsService
from grafomem.services.llm import LLMService
from grafomem.services.portal import PortalService
from grafomem.services.webhooks import WebhooksService

_DEFAULT_BASE_URL = "https://cloud.grafomem.com"


class GrafomemClient:
    """Python client for the GRAFOMEM Cloud API.

    Args:
        api_key: Your GRAFOMEM API key (``X-API-Key`` header).
        base_url: API base URL. Defaults to ``https://cloud.grafomem.com``.
            Use ``http://localhost:8080`` for local development.
        timeout: Default request timeout in seconds.
        max_retries: Maximum retries on 429/503.

    Example::

        client = GrafomemClient(api_key="gfm_abc123")
        store = client.stores.create(name="demo")
        client.memories.write(store.id, text="Hello world")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._http = HTTPTransport(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

        # Initialize all service modules
        self._stores = StoresService(self._http)
        self._memories = MemoriesService(self._http)
        self._governance = GovernanceService(self._http)
        self._orchestrator = OrchestratorService(self._http)
        self._decisions = DecisionsService(self._http)
        self._erasure = ErasureService(self._http)
        self._landing = _Landing(self._http)
        self._artifacts = ArtifactsService(self._http)
        self._reports = ReportsService(self._http)
        self._llm = LLMService(self._http)
        self._portal = PortalService(self._http)
        self._webhooks = WebhooksService(self._http)

    # ── Service Properties ────────────────────────────────────────────

    @property
    def stores(self) -> StoresService:
        """Memory store management (create, list, flush)."""
        return self._stores

    @property
    def memories(self) -> MemoriesService:
        """Memory record operations (write, retrieve, delete, supersede)."""
        return self._memories

    @property
    def governance(self) -> GovernanceService:
        """Policy-as-code governance (policies, evaluation, logs)."""
        return self._governance

    @property
    def orchestrator(self) -> OrchestratorService:
        """Agent orchestration (agents, workflows, receipts, replay)."""
        return self._orchestrator

    @property
    def decisions(self) -> DecisionsService:
        """Decision trail (inference audit log)."""
        return self._decisions

    @property
    def erasure(self) -> ErasureService:
        """GDPR erasure proof (certificates, verification)."""
        return self._erasure

    @property
    def landing(self) -> _Landing:
        """Landing zone conformance and certificate issuance."""
        return self._landing

    @property
    def artifacts(self) -> ArtifactsService:
        """Artifact registry service."""
        return self._artifacts

    @property
    def reports(self) -> ReportsService:
        """Regulatory compliance reports (EU AI Act, GDPR, DORA)."""
        return self._reports

    @property
    def llm(self) -> LLMService:
        """LLM provider and tool registry (BYOM)."""
        return self._llm

    @property
    def portal(self) -> PortalService:
        """Cloud Portal authentication (signup, login)."""
        return self._portal

    @property
    def webhooks(self) -> WebhooksService:
        """Webhook management (register, list, deliveries)."""
        return self._webhooks

    # ── Lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying HTTP connection."""
        self._http.close()

    def __enter__(self) -> "GrafomemClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"GrafomemClient(base_url={self._http._base_url!r})"
