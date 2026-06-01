"""
GRAFOMEM Audit Export Routes — Sprint 23.

FastAPI router providing compliance-ready export endpoints for
decisions, governance logs, and gcrumbs chain.

Endpoints:
    GET  /v1/audit/export/decisions   — Export decisions (CSV/JSON)
    GET  /v1/audit/export/governance  — Export governance logs (CSV/JSON)
    GET  /v1/audit/export/gcrumbs     — Export gcrumbs chain (JSON)
    GET  /v1/audit/export/full        — Combined audit package (ZIP)
    POST /v1/audit/export/pdf         — Generate compliance audit PDF
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

logger = logging.getLogger("grafomem.cloud.audit_export_routes")

router = APIRouter(tags=["Audit Export"])


# ------------------------------------------------------------------
# Dependency helpers
# ------------------------------------------------------------------

def _require_auth(request: Request) -> str:
    """Extract and validate tenant_id from the request."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Authentication required")
    return tenant_id


def _export_service(request: Request):
    """Get the AuditExportService from app state."""
    return getattr(request.app.state, "audit_export_service", None)


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get("/decisions")
async def export_decisions(
    request: Request,
    format: str = Query("json", pattern="^(json|csv)$"),
    date_from: str | None = Query(None, description="ISO datetime lower bound"),
    date_to: str | None = Query(None, description="ISO datetime upper bound"),
    tenant_id: str = Depends(_require_auth),
):
    """Export all decisions for the tenant as CSV or JSON.

    Response includes BLAKE2b-256 content hash in the
    ``X-Content-Hash`` header for tamper detection.
    """
    svc = _export_service(request)
    if not svc:
        return Response(
            content=b'{"error": "Audit export not configured"}',
            status_code=503,
            media_type="application/json",
        )

    result = svc.export_decisions(
        tenant_id, format=format,
        date_from=date_from, date_to=date_to,
    )
    return _export_response(result)


@router.get("/governance")
async def export_governance(
    request: Request,
    format: str = Query("json", pattern="^(json|csv)$"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    tenant_id: str = Depends(_require_auth),
):
    """Export governance evaluation logs as CSV or JSON."""
    svc = _export_service(request)
    if not svc:
        return Response(
            content=b'{"error": "Audit export not configured"}',
            status_code=503,
            media_type="application/json",
        )

    result = svc.export_governance(
        tenant_id, format=format,
        date_from=date_from, date_to=date_to,
    )
    return _export_response(result)


@router.get("/gcrumbs")
async def export_gcrumbs(
    request: Request,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    tenant_id: str = Depends(_require_auth),
):
    """Export the gcrumbs breadcrumb chain + epoch summary as JSON."""
    svc = _export_service(request)
    if not svc:
        return Response(
            content=b'{"error": "Audit export not configured"}',
            status_code=503,
            media_type="application/json",
        )

    result = svc.export_gcrumbs(
        tenant_id, date_from=date_from, date_to=date_to,
    )
    return _export_response(result)


@router.get("/full")
async def export_full(
    request: Request,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    tenant_id: str = Depends(_require_auth),
):
    """Export a full audit package as a ZIP file.

    Contains decisions.json, governance.json, gcrumbs.json, and manifest.json
    with per-file BLAKE2b content hashes.
    """
    svc = _export_service(request)
    if not svc:
        return Response(
            content=b'{"error": "Audit export not configured"}',
            status_code=503,
            media_type="application/json",
        )

    result = svc.export_full(
        tenant_id, date_from=date_from, date_to=date_to,
    )
    return _export_response(result)


@router.post("/pdf")
async def export_pdf(
    request: Request,
    tenant_id: str = Depends(_require_auth),
):
    """Generate a compliance audit PDF report.

    Renders the combined regulatory report (EU AI Act, GDPR, DORA)
    with governance logs, decision trail summary, and gcrumbs chain status.
    """
    # Use the existing compliance report + PDF renderer
    compliance = getattr(request.app.state, "compliance_reporter", None)
    if not compliance:
        return Response(
            content=b'{"error": "Compliance reporter not configured"}',
            status_code=503,
            media_type="application/json",
        )

    try:
        from aml.cloud.pdf_renderer import render_report_pdf
        report = compliance.generate_report(tenant_id, "full_audit")
        pdf_bytes = render_report_pdf(report)

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=audit_{tenant_id[:8]}.pdf",
            },
        )
    except Exception as e:
        logger.error("PDF export failed: %s", e)
        return Response(
            content=f'{{"error": "PDF generation failed: {e}"}}'.encode(),
            status_code=500,
            media_type="application/json",
        )


# ------------------------------------------------------------------
# Response helper
# ------------------------------------------------------------------

def _export_response(result) -> Response:
    """Build an HTTP response from an ExportResult."""
    headers = {
        "Content-Disposition": f"attachment; filename={result.filename}",
        "X-Content-Hash": result.metadata.content_hash,
        "X-Record-Count": str(result.metadata.record_count),
        "X-Export-Type": result.metadata.export_type,
        "X-Exported-At": result.metadata.exported_at,
    }
    if result.metadata.signature:
        headers["X-Signature"] = result.metadata.signature

    return Response(
        content=result.data,
        media_type=result.content_type,
        headers=headers,
    )
