"""Decision trail service.

Provides access to logged inference decisions, statistics, and NDJSON
export.

Usage::

    decisions = client.decisions.list(limit=10)
    detail = client.decisions.get(decisions[0].id)
    ndjson = client.decisions.export()
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from grafomem.types import DecisionRecord

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class DecisionsService:
    """Decision trail — signed inference audit log."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    def list(self, *, limit: int = 20) -> list[DecisionRecord]:
        """List recent decision records.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of :class:`DecisionRecord`.
        """
        data = self._http.get("/v1/decisions/", params={"limit": limit})
        items = data if isinstance(data, list) else data.get("decisions", [])
        return [DecisionRecord.model_validate(d) for d in items]

    def get(self, decision_id: str) -> DecisionRecord:
        """Get a single decision record."""
        data = self._http.get(f"/v1/decisions/{decision_id}")
        return DecisionRecord.model_validate(data)

    def stats(self) -> dict[str, Any]:
        """Get decision trail summary statistics."""
        data = self._http.get("/v1/decisions/stats")
        return data or {}

    def export(self) -> str:
        """Export all decisions as NDJSON.

        Returns:
            A string containing newline-delimited JSON.
        """
        raw = self._http.get_raw("/v1/decisions/export")
        return raw.decode("utf-8")
