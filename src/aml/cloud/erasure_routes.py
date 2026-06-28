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
    fact_content: str | None = None  # Content to hash (NOT stored)
    memory_deleted: bool = True
    legal_basis: str = "GDPR Article 17 — Right to Erasure"
    requested_by: str | None = "data_subject"
    signing_key: str | None = None  # hex-encoded Ed25519 seed (overrides service key)


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
        """Issue a signed erasure certificate for a deleted fact.

        Performs the full GDPR erasure workflow:
        1. Scrubs the fact from all decision trail records
        2. Computes a content hash (proof without retaining PII)
        3. Ed25519-signs the certificate
        4. Persists the certificate to the database

        Returns the signed erasure certificate.
        """
        tenant_id = _get_tenant_id(request)
        require_scope(request, "erasure:execute")

        signing_identity = None
        if req.signing_key:
            try:
                key_bytes = bytes.fromhex(req.signing_key)
            except ValueError:
                raise HTTPException(400, "signing_key must be hex-encoded")
            try:
                from aml.provenance import SigningIdentity
                signing_identity = SigningIdentity(key_bytes)
            except Exception as e:
                raise HTTPException(400, f"Invalid signing key: {e}")

        try:
            cert = erasure_service.issue_certificate(
                tenant_id=tenant_id,
                fact_ref=req.fact_ref,
                fact_content=req.fact_content,
                legal_basis=req.legal_basis,
                requested_by=req.requested_by,
                signing_identity=signing_identity,
            )
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
