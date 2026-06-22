"""
src/aml/cloud/landing_routes.py   (PHASE-B — WIRED to your erasure_routes pattern)

Factory: create_landing_router(landing_service) -> APIRouter, closing over the service
(exactly like create_erasure_router). Tenant via request.state.tenant.tenant_id.
"""
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from aml.server.scopes import require_scope

from .landing_service import LandingDenied, LandingPendingHITL, LandingError, LandingIssueRequest


class ConformanceRequest(BaseModel):
    artifact_ref: str
    layer_hashes: list
    data_provenance: dict

class IssueRequest(BaseModel):
    artifact_ref: str
    base_model_ref: str
    layer_hashes: list
    data_provenance: dict
    authority: dict
    conformance: dict
    permitted_actions: list
    kind: str = "lora+rag"

class ResumeRequest(BaseModel):
    approver: str


def _get_tenant_id(request: Request) -> str:
    """Mirror erasure_routes._get_tenant_id — tenant from the auth middleware."""
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return ctx.tenant_id


def create_landing_router(landing_service) -> APIRouter:
    router = APIRouter(prefix="/v1/landing", tags=["Landing"])

    @router.post("/conformance")
    async def run_conformance(req: ConformanceRequest, request: Request):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "compliance:admin")
        return landing_service.run_conformance(tenant_id, req.artifact_ref, [], req.data_provenance)

    @router.post("/issue", status_code=201)
    async def issue(req: IssueRequest, request: Request):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "compliance:admin")
        try:
            return landing_service.issue_certificate(tenant_id, LandingIssueRequest(**req.model_dump()))
        except LandingDenied as e:
            raise HTTPException(status_code=403, detail={"status": "denied", "certificate_id": str(e)})
        except LandingPendingHITL as e:
            raise HTTPException(status_code=202, detail={"status": "waiting_hitl", "certificate_id": e.certificate_id})
        except LandingError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/stats")
    async def stats(request: Request):
        require_scope(request, "compliance:read")
        return landing_service.get_stats(_get_tenant_id(request))

    @router.get("/{certificate_id}")
    async def get_one(certificate_id: str, request: Request):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "compliance:read")
        try:
            return landing_service.get_certificate(tenant_id, certificate_id)
        except LandingError:
            raise HTTPException(status_code=404, detail="certificate not found")

    @router.get("/{certificate_id}/verify")
    async def verify(certificate_id: str, request: Request):
        require_scope(request, "compliance:read")
        return landing_service.verify_certificate(_get_tenant_id(request), certificate_id)

    @router.get("")
    async def list_all(request: Request, limit: int = Query(50, le=200), offset: int = 0):
        require_scope(request, "compliance:read")
        return landing_service.list_certificates(_get_tenant_id(request), limit=limit, offset=offset)

    @router.post("/{certificate_id}/approve")
    async def approve(certificate_id: str, req: ResumeRequest, request: Request):
        require_scope(request, "compliance:admin")
        return landing_service.resume(_get_tenant_id(request), certificate_id, True, req.approver)

    @router.post("/{certificate_id}/reject")
    async def reject(certificate_id: str, req: ResumeRequest, request: Request):
        require_scope(request, "compliance:admin")
        return landing_service.resume(_get_tenant_id(request), certificate_id, False, req.approver)

    return router
