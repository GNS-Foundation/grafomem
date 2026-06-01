"""Pagination helpers for the GRAFOMEM SDK.

Provides ``Paginator`` (sync) and ``AsyncPaginator`` (async) that
automatically walk through paginated API responses.

Usage::

    # Sync
    from grafomem.pagination import Paginator
    for decision in Paginator(client.decisions.list, page_size=50):
        print(decision["decision_id"])

    # Async
    from grafomem.pagination import AsyncPaginator
    async for decision in AsyncPaginator(client.decisions.list, page_size=50):
        print(decision["decision_id"])
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Iterator


class Paginator:
    """Synchronous paginator over GRAFOMEM list endpoints.

    Assumes the endpoint accepts ``limit`` and ``offset`` kwargs and returns
    a dict with a list field (auto-detected) and an optional ``total`` count.

    Parameters
    ----------
    list_fn : Callable
        A bound service method like ``client.decisions.list``.
    page_size : int
        Number of items per page (default 50).
    max_items : int | None
        Stop after this many items (default: unlimited).
    list_key : str | None
        Key in the response dict containing the items list.
        If None, auto-detected from the first list-valued key.
    """

    def __init__(
        self,
        list_fn: Callable[..., Any],
        *,
        page_size: int = 50,
        max_items: int | None = None,
        list_key: str | None = None,
        **extra_params: Any,
    ) -> None:
        self._list_fn = list_fn
        self._page_size = page_size
        self._max_items = max_items
        self._list_key = list_key
        self._extra = extra_params

    def __iter__(self) -> Iterator[dict]:
        offset = 0
        yielded = 0

        while True:
            response = self._list_fn(
                limit=self._page_size, offset=offset, **self._extra,
            )

            items = self._extract_items(response)
            if not items:
                break

            for item in items:
                yield item
                yielded += 1
                if self._max_items and yielded >= self._max_items:
                    return

            if len(items) < self._page_size:
                break  # Last page

            offset += len(items)

    def _extract_items(self, response: Any) -> list[dict]:
        if not isinstance(response, dict):
            return []

        if self._list_key and self._list_key in response:
            return response[self._list_key]

        # Auto-detect: first list-valued key
        for key, value in response.items():
            if isinstance(value, list):
                self._list_key = key  # Cache for subsequent pages
                return value

        return []


class AsyncPaginator:
    """Asynchronous paginator over GRAFOMEM list endpoints.

    Same interface as ``Paginator`` but uses ``async for``.

    Usage::

        async for item in AsyncPaginator(client.decisions.list, page_size=20):
            process(item)
    """

    def __init__(
        self,
        list_fn: Callable[..., Any],
        *,
        page_size: int = 50,
        max_items: int | None = None,
        list_key: str | None = None,
        **extra_params: Any,
    ) -> None:
        self._list_fn = list_fn
        self._page_size = page_size
        self._max_items = max_items
        self._list_key = list_key
        self._extra = extra_params

    async def __aiter__(self) -> AsyncIterator[dict]:
        offset = 0
        yielded = 0

        while True:
            response = await self._list_fn(
                limit=self._page_size, offset=offset, **self._extra,
            )

            items = self._extract_items(response)
            if not items:
                break

            for item in items:
                yield item
                yielded += 1
                if self._max_items and yielded >= self._max_items:
                    return

            if len(items) < self._page_size:
                break  # Last page

            offset += len(items)

    def _extract_items(self, response: Any) -> list[dict]:
        if not isinstance(response, dict):
            return []

        if self._list_key and self._list_key in response:
            return response[self._list_key]

        for key, value in response.items():
            if isinstance(value, list):
                self._list_key = key
                return value

        return []
