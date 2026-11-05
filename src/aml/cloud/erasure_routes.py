"""
GRAFOMEM Erasure Proof API — REST endpoints for GDPR erasure certificates.

Provides endpoints to issue, list, verify, and download erasure certificates.
All endpoints are tenant-scoped via API key authentication.

Mounted at /v1/erasure when Cloud mode is active.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from aml.server.scopes import require_scope
from pydantic import BaseModel, Field

logger = logging.getLogger("grafomem.cloud.erasure_routes")


# ============================================================================
# Pydantic models
# ============================================================================

class IssueErasureRequest(BaseModel):
    """Request body for POST /v1/erasure/issue."""
    fact_ref: int
    # Content to hash (NOT stored). 0016: on this endpoint the fact is already gone, so
    # the server cannot observe it — this hash is CALLER-ASSERTED. The delete endpoint
    # (`POST /v1/stores/{id}/delete`) observes the content itself before deleting.
    fact_content: str | None = None
    legal_basis: str = "GDPR Article 17 — Right to Erasure"
    requested_by: str | None = "data_subject"
    # 0016: no caller-supplied signing key. A platform certificate is signed by the
    # platform's key only; `signing_key` was removed (it let a caller mint a
    # "platform-signed" certificate under a key of their choosing). `memory_deleted`
    # was removed too: it was accepted and ignored — this endpoint never deleted.
    # 0014: a client may DECLARE extra subsystems to record, but cannot assert their
    # status — they land as "unverified". Coverage itself is probed server-side.
    declared_subsystems: list[str] | None = None


class CertificateResponse(BaseModel):
    """Response model for an erasure certificate."""
    certificate_id: str
    tenant_id: str
    fact_ref: int
    fact_content_hash: str | None = None
    coverage: dict[str, str]
    scrubbed_decision_ids: list[str] = Field(default_factory=list)
    erasure_requested_at: str
    erasure_completed_at: str
    legal_basis: str
    requested_by: str | None = None
    signature: str | None = None  # base64
    public_key: str | None = None  # base64
    verified: bool = False
    verification_note: str | None = None


class VerifyResponse(BaseModel):
    """Response model for certificate verification."""
    valid: bool
    certificate_id: str
    detail: str


# ============================================================================
# Helper
# ============================================================================

def _cert_to_response(cert) -> CertificateResponse:
    """Convert an ErasureCertificate to a CertificateResponse."""
    return CertificateResponse(
        certificate_id=cert.certificate_id,
        tenant_id=cert.tenant_id,
        fact_ref=cert.fact_ref,
        fact_content_hash=cert.fact_content_hash,
        coverage=cert.coverage,
        scrubbed_decision_ids=cert.scrubbed_decision_ids,
        erasure_requested_at=cert.erasure_requested_at.isoformat(),
        erasure_completed_at=cert.erasure_completed_at.isoformat(),
        legal_basis=cert.legal_basis,
        requested_by=cert.requested_by,
        signature=base64.b64encode(cert.signature).decode() if cert.signature else None,
        public_key=base64.b64encode(cert.public_key).decode() if cert.public_key else None,
        verified=cert.verified,
        verification_note=cert.verification_note,
    )


def _get_tenant_id(request: Request) -> str:
    """Extract tenant_id from auth middleware. Raises 401 if not authenticated."""
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


# ============================================================================
# Router factory
# ============================================================================

def create_erasure_router(erasure_service) -> APIRouter:
    """Create the Erasure Proof FastAPI router.

    Parameters
    ----------
    erasure_service : ErasureProofService
        The core erasure proof service.
    """
    router = APIRouter(prefix="/v1/erasure", tags=["Erasure Proof"])

    # ------------------------------------------------------------------
    # POST /v1/erasure/issue — issue an erasure certificate
    # ------------------------------------------------------------------

    @router.post("/issue", response_model=CertificateResponse)
    async def issue_certificate(req: IssueErasureRequest, request: Request):
        """Issue a signed erasure certificate for a fact that was ALREADY deleted.

        This endpoint does not delete anything. It:
        1. Probes coverage server-side (0014) — and REFUSES with 409 if the primary
           store still holds the fact (0016): no erasure certificate for a non-erasure.
        2. Scrubs the fact's content from decision trail records
        3. Records the caller-asserted content hash, if `fact_content` was supplied
        4. Ed25519-signs the certificate with the platform key
        5. Persists the certificate to the database

        To delete AND certify in one step, with a server-observed content digest, use
        `POST /v1/stores/{store_id}/delete`.
        """
        tenant_id = _get_tenant_id(request)
        require_scope(request, "erasure:execute")

        from aml.cloud.erasure_proof import (
            CertificateNotIssued, LedgerRequired, EmptyGovernanceCoverage, UnsignedErasure,
            FactStillPresent,
        )
        from fastapi.responses import JSONResponse
        try:
            cert = erasure_service.issue_certificate(
                tenant_id=tenant_id,
                fact_ref=req.fact_ref,
                fact_content=req.fact_content,
                declared_subsystems=req.declared_subsystems,
                legal_basis=req.legal_basis,
                requested_by=req.requested_by,
            )
        except FactStillPresent as e:
            # 0016: the fact is still in the primary store. Nothing issued, nothing ledgered.
            raise HTTPException(409, {
                "status": "not_issued",
                "reason": "fact_still_present",
                "fact_ref": req.fact_ref,
                "detail": str(e),
            })
        except CertificateNotIssued:
            # ERASURE_LEDGER_OPTIONAL (dev/test): the fact was erased, but with no ledger
            # configured no certificate is issued (0013). NOT a 500 — the erasure succeeded.
            return JSONResponse(
                status_code=200,
                content={"status": "erased_no_certificate", "certificate_id": None,
                         "detail": "erased, no certificate issued: ledger not configured"},
            )
        except LedgerRequired:
            # Default mode, ledger unavailable: nothing was erased, nothing issued.
            raise HTTPException(503, "restore-scrub ledger unavailable — erasure not performed, no certificate issued")
        except EmptyGovernanceCoverage as e:
            raise HTTPException(400, str(e))
        except UnsignedErasure:
            raise HTTPException(503, "unable to sign the erasure certificate — certificate not issued")
        except Exception as e:
            logger.error("Failed to issue erasure certificate: %s", e)
            raise HTTPException(500, f"Failed to issue certificate: {e}")

        return _cert_to_response(cert)

    # ------------------------------------------------------------------
    # GET /v1/erasure/stats — summary stats
    # ------------------------------------------------------------------

    @router.get("/stats")
    async def erasure_stats(request: Request):
        """Summary statistics for erasure certificates."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "erasure:execute")
        return erasure_service.get_stats(tenant_id)

    # ------------------------------------------------------------------
    # GET /v1/erasure/ledger/verify — verify the ledger hash chain (0017)
    # Declared BEFORE the /{certificate_id} routes so "ledger" is never captured
    # as a certificate id. Scope: erasure:execute — the only erasure scope that
    # exists today (every erasure read uses it); a read-only scope is a separate
    # decision. The ledger is global (all tenants), so the verdict is too: it
    # reports chain integrity, never another tenant's content.
    # ------------------------------------------------------------------

    @router.get("/ledger/verify")
    async def verify_ledger_chain(request: Request):
        """Verify the erasure ledger's hash chain end to end.

        Returns ``{"ok": bool, "rows": n, "problems": [...]}``. ``problems`` names
        each break with its ``seq`` so an auditor can localise it. A ledger that
        was never sealed (migration 021 not applied) is reported as not ok.
        """
        require_scope(request, "erasure:execute")
        try:
            ok, problems, rows = erasure_service.verify_ledger()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        return {"ok": ok, "rows": rows, "problems": problems}

    # ------------------------------------------------------------------
    # GET /v1/erasure/{certificate_id} — get a certificate
    # ------------------------------------------------------------------

    @router.get("/{certificate_id}", response_model=CertificateResponse)
    async def get_certificate(certificate_id: str, request: Request):
        """Retrieve a single erasure certificate by its ID."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "erasure:execute")
        cert = erasure_service.get(certificate_id)

        if cert is None:
            raise HTTPException(404, f"Certificate '{certificate_id}' not found")
        if cert.tenant_id != tenant_id:
            raise HTTPException(404, f"Certificate '{certificate_id}' not found")

        return _cert_to_response(cert)

    # ------------------------------------------------------------------
    # GET /v1/erasure/{certificate_id}/verify — verify signature
    # ------------------------------------------------------------------

    @router.get("/{certificate_id}/verify", response_model=VerifyResponse)
    async def verify_certificate(certificate_id: str, request: Request):
        """Verify the Ed25519 signature on an erasure certificate.

        Returns whether the certificate is authentic and untampered.
        """
        tenant_id = _get_tenant_id(request)
        require_scope(request, "erasure:execute")

        # Check ownership first
        cert = erasure_service.get(certificate_id)
        if cert is None or cert.tenant_id != tenant_id:
            raise HTTPException(404, f"Certificate '{certificate_id}' not found")

        result = erasure_service.verify_certificate(certificate_id)
        return VerifyResponse(**result)

    # ------------------------------------------------------------------
    # GET /v1/erasure/fact/{fact_ref} — find certificate for a fact
    # ------------------------------------------------------------------

    @router.get("/fact/{fact_ref}", response_model=CertificateResponse)
    async def get_by_fact(fact_ref: int, request: Request):
        """Find the erasure certificate for a specific fact ref."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "erasure:execute")
        cert = erasure_service.get_by_fact(tenant_id, fact_ref)

        if cert is None:
            raise HTTPException(404, f"No erasure certificate for fact ref {fact_ref}")

        return _cert_to_response(cert)

    # ------------------------------------------------------------------
    # GET /v1/erasure/ — list all certificates
    # ------------------------------------------------------------------

    @router.get("/")
    async def list_certificates(
        request: Request,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        """List all erasure certificates for the tenant."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "erasure:execute")

        certs = erasure_service.list_certificates(
            tenant_id, limit=limit, offset=offset,
        )

        return {
            "certificates": [_cert_to_response(c) for c in certs],
            "count": len(certs),
            "limit": limit,
            "offset": offset,
        }

    return router
