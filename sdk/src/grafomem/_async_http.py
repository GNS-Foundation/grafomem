"""
Async HTTP transport for GRAFOMEM SDK v2.

Mirror of ``_http.HTTPTransport`` but using ``httpx.AsyncClient``
for non-blocking I/O.  Every method is ``async def``.

Usage::

    async with AsyncHTTPTransport(base_url, api_key) as t:
        data = await t.get("/v1/stores")
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("grafomem.sdk.async_http")

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_RETRIES = 3


class AsyncHTTPTransport:
    """Async HTTP transport layer for the GRAFOMEM API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_RETRIES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "grafomem-sdk/0.2.0 (async)",
                },
                timeout=self._timeout,
            )
        return self._client

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return await self._request("POST", path, json=json, **kwargs)

    async def put(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return await self._request("PUT", path, json=json, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self._request("DELETE", path, **kwargs)

    async def get_raw(self, path: str, **kwargs: Any) -> bytes:
        resp = await self._raw_request("GET", path, **kwargs)
        return resp.content

    # ------------------------------------------------------------------
    # Core request methods
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._raw_request(method, path, **kwargs)
        if resp.status_code == 204:
            return {}
        return resp.json()

    async def _raw_request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        client = self._ensure_client()

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await client.request(method, path, **kwargs)
                self._raise_for_status(resp)
                return resp
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 500, 502, 503, 504):
                    last_exc = e
                    logger.warning(
                        "Retryable error %d on %s %s (attempt %d/%d)",
                        e.response.status_code, method, path,
                        attempt + 1, self._max_retries,
                    )
                    import asyncio
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                raise
            except httpx.RequestError as e:
                last_exc = e
                logger.warning(
                    "Request error on %s %s: %s (attempt %d/%d)",
                    method, path, e, attempt + 1, self._max_retries,
                )
                import asyncio
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue

        raise last_exc or RuntimeError("Request failed after retries")

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Raise ``httpx.HTTPStatusError`` for 4xx/5xx responses."""
        if resp.status_code >= 400:
            from grafomem._http import _format_error
            try:
                detail = _format_error(resp)
            except Exception:
                detail = resp.text[:200]
            raise httpx.HTTPStatusError(
                message=f"GRAFOMEM API error: {detail}",
                request=resp.request,
                response=resp,
            )

    # ------------------------------------------------------------------
    # SSE streaming
    # ------------------------------------------------------------------

    async def stream_post(
        self, path: str, json: Any = None
    ) -> AsyncIterator[dict[str, str]]:
        """Stream SSE events from a POST endpoint.

        Yields dicts with ``event`` and ``data`` keys.
        """
        client = self._ensure_client()
        async with client.stream(
            "POST", path, json=json,
            headers={"Accept": "text/event-stream"},
            timeout=None,
        ) as resp:
            self._raise_for_status(resp)
            event_type = ""
            data_lines: list[str] = []
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "":
                    if data_lines:
                        yield {
                            "event": event_type,
                            "data": "\n".join(data_lines),
                        }
                    event_type = ""
                    data_lines = []

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> "AsyncHTTPTransport":
        self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
"""
GRAFOMEM SDK v2 — Async HTTP Transport.

Mirrors HTTPTransport with async/await support.
"""
