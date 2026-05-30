"""HTTP transport layer for the GRAFOMEM SDK.

Wraps :mod:`httpx` to provide:
- Base URL configuration
- ``X-API-Key`` header injection
- Automatic retry on 429 / 503
- JSON error → typed exception mapping
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from grafomem.errors import (
    APIError,
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)

_DEFAULT_TIMEOUT = 30.0
_WORKFLOW_TIMEOUT = 300.0
_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.0  # seconds, doubles each retry


class HTTPTransport:
    """Low-level HTTP client for GRAFOMEM Cloud API.

    Args:
        base_url: The API base URL (e.g. ``https://cloud.grafomem.com``).
        api_key: The ``X-API-Key`` header value. May be ``None`` for
            unauthenticated portal endpoints (signup/login).
        timeout: Default request timeout in seconds.
        max_retries: Maximum number of retries on 429/503.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries

        headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"

        self._client = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )

    # ── Public helpers ────────────────────────────────────────────────

    def get(self, path: str, **kwargs: Any) -> Any:
        """Send a GET request and return parsed JSON."""
        return self._request("GET", path, **kwargs)

    def post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        """Send a POST request with a JSON body and return parsed JSON."""
        return self._request("POST", path, json=json, **kwargs)

    def put(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        """Send a PUT request with a JSON body and return parsed JSON."""
        return self._request("PUT", path, json=json, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        """Send a DELETE request and return parsed JSON (or None)."""
        return self._request("DELETE", path, **kwargs)

    def get_raw(self, path: str, **kwargs: Any) -> bytes:
        """Send a GET request and return raw bytes (for downloads)."""
        resp = self._raw_request("GET", path, **kwargs)
        return resp.content

    # ── Internal ──────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute a request with retry logic and return parsed JSON."""
        resp = self._raw_request(method, path, **kwargs)

        # Some endpoints return empty body on success (DELETE, flush)
        if resp.status_code == 204 or not resp.content:
            return None

        try:
            return resp.json()
        except Exception:
            return None

    def _raw_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a request with retry logic, raise on error."""
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_exc = APIError(408, f"Request timed out: {exc}")
                if attempt < self._max_retries:
                    time.sleep(_RETRY_BACKOFF * (2 ** attempt))
                    continue
                raise last_exc from exc

            # Success
            if resp.is_success:
                return resp

            # Retryable errors
            if resp.status_code in (429, 503) and attempt < self._max_retries:
                retry_after = float(resp.headers.get("Retry-After", _RETRY_BACKOFF * (2 ** attempt)))
                time.sleep(retry_after)
                continue

            # Non-retryable errors — raise typed exception
            self._raise_for_status(resp)

        # Should not reach here, but just in case
        if last_exc:
            raise last_exc
        raise APIError(500, "Max retries exceeded")

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Map HTTP status codes to typed SDK exceptions."""
        detail = ""
        body = None

        try:
            body = resp.json()
            detail = body.get("detail", "") or body.get("message", "") or str(body)
        except Exception:
            detail = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"

        status = resp.status_code

        if status in (401, 403):
            raise AuthenticationError(detail)
        elif status == 404:
            raise NotFoundError(detail)
        elif status == 422:
            raise ValidationError(detail, body=body)
        elif status == 429:
            raise RateLimitError(detail)
        else:
            raise APIError(status, detail, body=body)

    def stream_post(
        self, path: str, json: Any = None, *, timeout: float = 300.0,
    ) -> httpx.Response:
        """Send a POST and return a *streaming* response (for SSE).

        The caller is responsible for iterating over the response and
        closing it.  Typical usage::

            resp = transport.stream_post("/v1/…/stream", json={...})
            with resp:
                for line in resp.iter_lines():
                    ...

        Returns:
            An httpx.Response in streaming mode.
        """
        headers = {"Accept": "text/event-stream"}
        resp = self._client.send(
            self._client.build_request(
                "POST", path, json=json, headers=headers,
            ),
            stream=True,
            timeout=httpx.Timeout(timeout),
        )
        if not resp.is_success:
            # Consume the body so we can inspect the error
            resp.read()
            self._raise_for_status(resp)
        return resp

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "HTTPTransport":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
