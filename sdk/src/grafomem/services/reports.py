"""Regulatory reports service.

Generate compliance reports for EU AI Act, GDPR, DORA, and full audit.

Usage::

    report = client.reports.generate(framework="eu_ai_act")
    for section in report.sections:
        print(f"{section.article}: {section.status}")
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from grafomem.types import Report

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class ReportsService:
    """Regulatory compliance report generation."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    def generate(self, framework: str = "eu_ai_act") -> Report:
        """Generate a compliance report.

        Args:
            framework: One of ``"eu_ai_act"``, ``"gdpr"``, ``"dora"``,
                or ``"full_audit"``.

        Returns:
            A :class:`Report` with sections and compliance status.
        """
        data = self._http.post("/v1/reports/generate", json={"framework": framework})
        return Report.model_validate(data)

    def list(self) -> list[Report]:
        """List all generated reports."""
        data = self._http.get("/v1/reports/")
        items = data if isinstance(data, list) else data.get("reports", [])
        return [Report.model_validate(r) for r in items]

    def get(self, report_id: str) -> Report:
        """Get a report with full content."""
        data = self._http.get(f"/v1/reports/{report_id}")
        return Report.model_validate(data)

    def download(self, report_id: str) -> bytes:
        """Download a report as a JSON file.

        Returns:
            Raw bytes of the report file.
        """
        return self._http.get_raw(f"/v1/reports/{report_id}/download")

    def download_pdf(self, report_id: str) -> bytes:
        """Download a report as a styled PDF document.

        Returns:
            Raw bytes of the PDF file.
        """
        return self._http.get_raw(f"/v1/reports/{report_id}/download/pdf")

    def frameworks(self) -> list[str]:
        """Get available report frameworks."""
        data = self._http.get("/v1/reports/frameworks")
        return data if isinstance(data, list) else data.get("frameworks", [])

    def stats(self) -> dict[str, Any]:
        """Get report generation statistics."""
        data = self._http.get("/v1/reports/stats")
        return data or {}
