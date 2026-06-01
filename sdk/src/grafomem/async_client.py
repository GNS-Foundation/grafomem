"""GRAFOMEM Cloud async client (SDK v2).

The :class:`GrafomemAsyncClient` mirrors :class:`GrafomemClient` but
every service method is ``async def``.

Usage::

    from grafomem import GrafomemAsyncClient

    async with GrafomemAsyncClient(api_key="gfm_...") as client:
        stores = await client.stores.list()
"""

from __future__ import annotations

from typing import Any

from grafomem._async_http import AsyncHTTPTransport

_DEFAULT_BASE_URL = "https://cloud.grafomem.com"


# ---------------------------------------------------------------------------
# Thin async service wrappers — each delegates to AsyncHTTPTransport
# ---------------------------------------------------------------------------

class _AsyncService:
    """Base class for async service wrappers."""

    def __init__(self, http: AsyncHTTPTransport, prefix: str) -> None:
        self._http = http
        self._prefix = prefix.rstrip("/")

    def _path(self, suffix: str = "") -> str:
        return f"{self._prefix}/{suffix}" if suffix else self._prefix


class AsyncStoresService(_AsyncService):
    def __init__(self, http: AsyncHTTPTransport) -> None:
        super().__init__(http, "/v1/stores")

    async def list(self, **params: Any) -> Any:
        return await self._http.get(self._path(), params=params)

    async def create(self, name: str = "default", **kw: Any) -> Any:
        return await self._http.post(self._path(), json={"name": name, **kw})

    async def get(self, store_id: str) -> Any:
        return await self._http.get(self._path(store_id))


class AsyncMemoriesService(_AsyncService):
    def __init__(self, http: AsyncHTTPTransport) -> None:
        super().__init__(http, "/v1/stores")

    async def write(self, store_id: str, *, text: str, **kw: Any) -> Any:
        return await self._http.post(f"{self._prefix}/{store_id}/write",
                                      json={"content": text, **kw})

    async def retrieve(self, store_id: str, *, query: str,
                       limit: int = 5, **kw: Any) -> Any:
        return await self._http.post(
            f"{self._prefix}/{store_id}/retrieve",
            json={"query": query, "limit": limit, **kw},
        )

    async def delete(self, store_id: str, *, ref: int) -> Any:
        return await self._http.post(f"{self._prefix}/{store_id}/delete",
                                      json={"ref": ref})


class AsyncGovernanceService(_AsyncService):
    def __init__(self, http: AsyncHTTPTransport) -> None:
        super().__init__(http, "/v1/governance")

    async def evaluate(self, operation: str, context: dict, **kw: Any) -> Any:
        return await self._http.post(self._path("evaluate"),
                                      json={"operation": operation,
                                            "context": context, **kw})

    async def list_policies(self, **params: Any) -> Any:
        return await self._http.get(self._path("policies"), params=params)

    async def create_policy(self, **payload: Any) -> Any:
        return await self._http.post(self._path("policies"), json=payload)

    async def delete_policy(self, policy_id: str) -> Any:
        return await self._http.delete(self._path(f"policies/{policy_id}"))


class AsyncDecisionsService(_AsyncService):
    def __init__(self, http: AsyncHTTPTransport) -> None:
        super().__init__(http, "/v1/decisions")

    async def list(self, *, limit: int = 20, offset: int = 0, **kw: Any) -> Any:
        return await self._http.get(self._path(),
                                     params={"limit": limit, "offset": offset, **kw})

    async def get(self, decision_id: str) -> Any:
        return await self._http.get(self._path(decision_id))

    async def stats(self) -> Any:
        return await self._http.get(self._path("stats"))


class AsyncErasureService(_AsyncService):
    def __init__(self, http: AsyncHTTPTransport) -> None:
        super().__init__(http, "/v1/erasure")

    async def issue(self, **payload: Any) -> Any:
        return await self._http.post(self._path("issue"), json=payload)

    async def verify(self, certificate_id: str) -> Any:
        return await self._http.get(self._path(f"{certificate_id}/verify"))

    async def list(self, **params: Any) -> Any:
        return await self._http.get(self._path(), params=params)


class AsyncOrchestratorService(_AsyncService):
    def __init__(self, http: AsyncHTTPTransport) -> None:
        super().__init__(http, "/v1/orchestrator")

    async def create_agent(self, **payload: Any) -> Any:
        return await self._http.post(self._path("agents"), json=payload)

    async def list_agents(self, **params: Any) -> Any:
        return await self._http.get(self._path("agents"), params=params)

    async def create_workflow(self, **payload: Any) -> Any:
        return await self._http.post(self._path("workflows"), json=payload)

    async def run_workflow(self, workflow_id: str, **payload: Any) -> Any:
        return await self._http.post(
            self._path(f"workflows/{workflow_id}/run"), json=payload,
        )


class AsyncReportsService(_AsyncService):
    def __init__(self, http: AsyncHTTPTransport) -> None:
        super().__init__(http, "/v1/reports")

    async def generate(self, framework: str = "eu_ai_act", **kw: Any) -> Any:
        return await self._http.post(self._path("generate"),
                                      json={"framework": framework, **kw})

    async def list(self, **params: Any) -> Any:
        return await self._http.get(self._path(), params=params)


class AsyncLLMService(_AsyncService):
    def __init__(self, http: AsyncHTTPTransport) -> None:
        super().__init__(http, "/v1/llm")

    async def list_providers(self) -> Any:
        return await self._http.get(self._path("providers"))

    async def register_provider(self, **payload: Any) -> Any:
        return await self._http.post(self._path("providers"), json=payload)


class AsyncWebhooksService(_AsyncService):
    def __init__(self, http: AsyncHTTPTransport) -> None:
        super().__init__(http, "/v1/webhooks")

    async def list(self, **params: Any) -> Any:
        return await self._http.get(self._path(), params=params)

    async def create(self, **payload: Any) -> Any:
        return await self._http.post(self._path(), json=payload)

    async def deliveries(self, webhook_id: str) -> Any:
        return await self._http.get(self._path(f"{webhook_id}/deliveries"))


# ---------------------------------------------------------------------------
# Main async client
# ---------------------------------------------------------------------------

class GrafomemAsyncClient:
    """Async Python client for the GRAFOMEM Cloud API.

    Args:
        api_key: Your GRAFOMEM API key.
        base_url: API base URL.
        timeout: Default request timeout in seconds.
        max_retries: Maximum retries on 429/503.

    Example::

        async with GrafomemAsyncClient(api_key="gfm_abc123") as client:
            store = await client.stores.create(name="demo")
            await client.memories.write(store["store_id"], text="Hello")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._http = AsyncHTTPTransport(
            base_url=base_url,
            api_key=api_key or "",
            timeout=timeout,
            max_retries=max_retries,
        )

        self._stores = AsyncStoresService(self._http)
        self._memories = AsyncMemoriesService(self._http)
        self._governance = AsyncGovernanceService(self._http)
        self._orchestrator = AsyncOrchestratorService(self._http)
        self._decisions = AsyncDecisionsService(self._http)
        self._erasure = AsyncErasureService(self._http)
        self._reports = AsyncReportsService(self._http)
        self._llm = AsyncLLMService(self._http)
        self._webhooks = AsyncWebhooksService(self._http)

    # ── Service Properties ────────────────────────────────────────────

    @property
    def stores(self) -> AsyncStoresService:
        return self._stores

    @property
    def memories(self) -> AsyncMemoriesService:
        return self._memories

    @property
    def governance(self) -> AsyncGovernanceService:
        return self._governance

    @property
    def orchestrator(self) -> AsyncOrchestratorService:
        return self._orchestrator

    @property
    def decisions(self) -> AsyncDecisionsService:
        return self._decisions

    @property
    def erasure(self) -> AsyncErasureService:
        return self._erasure

    @property
    def reports(self) -> AsyncReportsService:
        return self._reports

    @property
    def llm(self) -> AsyncLLMService:
        return self._llm

    @property
    def webhooks(self) -> AsyncWebhooksService:
        return self._webhooks

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "GrafomemAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"GrafomemAsyncClient(base_url={self._http._base_url!r})"
