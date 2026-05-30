"""Exception hierarchy for the GRAFOMEM SDK.

All exceptions inherit from :class:`GrafomemError` so callers can
catch a single base class when they don't need granularity.
"""

from __future__ import annotations


class GrafomemError(Exception):
    """Base exception for all GRAFOMEM SDK errors."""


class APIError(GrafomemError):
    """The API returned a non-2xx status code.

    Attributes:
        status_code: HTTP status code.
        detail: Human-readable error message from the API.
        body: Raw response body (if available).
    """

    def __init__(
        self,
        status_code: int,
        detail: str = "",
        body: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.body = body
        super().__init__(f"HTTP {status_code}: {detail}")


class AuthenticationError(APIError):
    """401 or 403 — invalid or missing API key."""

    def __init__(self, detail: str = "Authentication failed") -> None:
        super().__init__(status_code=401, detail=detail)


class NotFoundError(APIError):
    """404 — requested resource does not exist."""

    def __init__(self, detail: str = "Resource not found") -> None:
        super().__init__(status_code=404, detail=detail)


class RateLimitError(APIError):
    """429 — too many requests."""

    def __init__(self, detail: str = "Rate limit exceeded") -> None:
        super().__init__(status_code=429, detail=detail)


class ValidationError(APIError):
    """422 — request body failed validation."""

    def __init__(self, detail: str = "Validation error", body: dict | None = None) -> None:
        super().__init__(status_code=422, detail=detail, body=body)
