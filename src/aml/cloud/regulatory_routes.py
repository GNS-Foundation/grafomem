"""
GRAFOMEM Regulatory Reports API — REST endpoints for compliance reports.

Provides endpoints to generate, list, view, download, and delete
regulatory compliance reports. All endpoints are tenant-scoped.

Mounted at /v1/reports when Cloud mode is active.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

logger = logging.getLogger("grafomem.cloud.regulatory_routes")

from aml.cloud.schemas import (
    ReportResponse,
    ReportListResponse,
    ReportStatsResponse,
)


# ============================================================================
# Pydantic models
# ============================================================================

class GenerateReportRequest(BaseModel):
    report_type: str = "full_audit"  # eu_ai_act, gdpr, dora, full_audit
    period_days: int = Field(30, ge=1, le=365)


# ============================================================================
# Helper
# ============================================================================

def _get_tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


# ============================================================================
# Router factory
# ============================================================================

def create_regulatory_router(report_service) -> APIRouter:
    """Create the Regulatory Reports FastAPI router."""

    router = APIRouter(prefix="/v1/reports", tags=["Regulatory Reports"])

    # ------------------------------------------------------------------
    # GET /v1/reports/stats — summary stats
    # ------------------------------------------------------------------

    @router.get("/stats", response_model=ReportStatsResponse)
    async def report_stats(request: Request):
        """Summary statistics for regulatory reports."""
        tenant_id = _get_tenant_id(request)
        return report_service.get_stats(tenant_id)

    # ------------------------------------------------------------------
    # GET /v1/reports/frameworks — available frameworks
    # ------------------------------------------------------------------

    @router.get("/frameworks")
    async def list_frameworks():
        """List available regulatory frameworks."""
        return {
            "frameworks": [
                {
                    "type": "eu_ai_act",
                    "name": "EU AI Act",
                    "regulation": "Regulation (EU) 2024/1689",
                    "articles": ["Article 13 (Transparency)", "Article 14 (Human Oversight)", "Article 15 (Accuracy)"],
                },
                {
                    "type": "gdpr",
                    "name": "GDPR",
                    "regulation": "Regulation (EU) 2016/679",
                    "articles": ["Article 17 (Erasure)", "Article 25 (By Design)", "Article 30 (Records)"],
                },
                {
                    "type": "dora",
                    "name": "DORA",
                    "regulation": "Regulation (EU) 2022/2554",
                    "articles": ["Article 6 (ICT Risk)", "Article 9 (Protection)", "Article 11 (Audit Trail)"],
                },
                {
                    "type": "full_audit",
                    "name": "Full Audit Package",
                    "regulation": "All frameworks combined",
                    "articles": [],
                },
            ]
        }

    # ------------------------------------------------------------------
    # POST /v1/reports/generate — generate a report
    # ------------------------------------------------------------------

    @router.post("/generate", response_model=ReportResponse)
    async def generate_report(req: GenerateReportRequest, request: Request):
        """Generate a new regulatory compliance report."""
        tenant_id = _get_tenant_id(request)

        try:
            report = report_service.generate(
                tenant_id=tenant_id,
                report_type=req.report_type,
                period_days=req.period_days,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            logger.error("Failed to generate report: %s", e)
            raise HTTPException(500, f"Report generation failed: {e}")

        return report_service.report_to_dict(report)

    # ------------------------------------------------------------------
    # GET /v1/reports/ — list reports
    # ------------------------------------------------------------------

    @router.get("/", response_model=ReportListResponse)
    async def list_reports(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        """List all regulatory reports for the tenant."""
        tenant_id = _get_tenant_id(request)
        reports = report_service.list_reports(tenant_id, limit=limit, offset=offset)
        return {
            "reports": [report_service.report_summary(r) for r in reports],
            "count": len(reports),
        }

    # ------------------------------------------------------------------
    # GET /v1/reports/{report_id} — get full report
    # ------------------------------------------------------------------

    @router.get("/{report_id}", response_model=ReportResponse)
    async def get_report(report_id: str, request: Request):
        """Retrieve a full regulatory report."""
        tenant_id = _get_tenant_id(request)
        report = report_service.get(report_id)

        if report is None or report.tenant_id != tenant_id:
            raise HTTPException(404, f"Report '{report_id}' not found")

        return report_service.report_to_dict(report)

    # ------------------------------------------------------------------
    # GET /v1/reports/{report_id}/download — download as JSON
    # ------------------------------------------------------------------

    @router.get("/{report_id}/download")
    async def download_report(report_id: str, request: Request):
        """Download the full report as a JSON file."""
        tenant_id = _get_tenant_id(request)
        report = report_service.get(report_id)

        if report is None or report.tenant_id != tenant_id:
            raise HTTPException(404, f"Report '{report_id}' not found")

        content = json.dumps(
            report_service.report_to_dict(report),
            indent=2, ensure_ascii=False,
        )
        filename = f"grafomem_{report.report_type.value}_{report.created_at.strftime('%Y%m%d')}.json"

        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # ------------------------------------------------------------------
    # GET /v1/reports/{report_id}/download/pdf — download as PDF
    # ------------------------------------------------------------------

    @router.get("/{report_id}/download/pdf")
    async def download_report_pdf(report_id: str, request: Request):
        """Download the report as a styled PDF document."""
        tenant_id = _get_tenant_id(request)
        report = report_service.get(report_id)

        if report is None or report.tenant_id != tenant_id:
            raise HTTPException(404, f"Report '{report_id}' not found")

        try:
            from aml.cloud.pdf_renderer import render_report_pdf
            pdf_bytes = render_report_pdf(report)
        except ImportError:
            raise HTTPException(
                501,
                "PDF export requires fpdf2. Install with: pip install fpdf2",
            )
        except Exception as e:
            logger.error("PDF rendering failed: %s", e)
            raise HTTPException(500, f"PDF rendering failed: {e}")

        filename = (
            f"grafomem_{report.report_type.value}"
            f"_{report.created_at.strftime('%Y%m%d')}.pdf"
        )

        return Response(
            content=bytes(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # ------------------------------------------------------------------
    # DELETE /v1/reports/{report_id}
    # ------------------------------------------------------------------

    @router.delete("/{report_id}")
    async def delete_report(report_id: str, request: Request):
        """Delete a regulatory report."""
        tenant_id = _get_tenant_id(request)
        deleted = report_service.delete(report_id, tenant_id)

        if not deleted:
            raise HTTPException(404, f"Report '{report_id}' not found")

        return {"deleted": True, "report_id": report_id}

    return router
