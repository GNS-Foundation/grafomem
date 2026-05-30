"""
src/aml/cloud/artifact_registry_routes.py   (R1)

Factory: create_artifact_registry_router(service) -> APIRouter (mirrors create_landing_router).
Tenant via request.state.tenant.tenant_id.
"""
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from .artifact_registry import (
    RegistryDenied, RegistryPendingHITL, RegistryError, ArtifactRegisterRequest,
)


class RegisterRequest(BaseModel):
    artifact_ref: str
    base_model_ref: str
    layers: list                # [{media_type, digest, size}]
    kind: str = "lora+rag"
    metadata: dict = {}

class IntegrityRequest(BaseModel):
    layer_hashes: list

class CertifyRequest(BaseModel):
    certificate_id: str

class ResumeRequest(BaseModel):
    approver: str


def _get_tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return ctx.tenant_id


def create_artifact_registry_router(service) -> APIRouter:
    router = APIRouter(prefix="/v1/artifacts", tags=["Artifact Registry"])

    @router.post("/register", status_code=201)
    async def register(req: RegisterRequest, request: Request):
        tenant_id = _get_tenant_id(request)
        try:
            return service.register(tenant_id, ArtifactRegisterRequest(**req.model_dump()))
        except RegistryDenied as e:
            raise HTTPException(status_code=403, detail={"status": "denied", "artifact_id": str(e)})
        except RegistryPendingHITL as e:
            raise HTTPException(status_code=202, detail={"status": "waiting_hitl", "artifact_id": e.artifact_id})
        except RegistryError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/stats")
    async def stats(request: Request):
        return service.get_stats(_get_tenant_id(request))

    @router.get("/{artifact_id}")
    async def get_one(artifact_id: str, request: Request):
        try:
            return service.get(_get_tenant_id(request), artifact_id)
        except RegistryError:
            raise HTTPException(status_code=404, detail="artifact not found")

    @router.get("/{artifact_id}/verify")
    async def verify(artifact_id: str, request: Request):
        return service.verify(_get_tenant_id(request), artifact_id)

    @router.post("/{artifact_id}/integrity")
    async def integrity(artifact_id: str, req: IntegrityRequest, request: Request):
        return service.check_integrity(_get_tenant_id(request), artifact_id, req.layer_hashes)

    @router.post("/{artifact_id}/certify")
    async def certify(artifact_id: str, req: CertifyRequest, request: Request):
        return service.certify(_get_tenant_id(request), artifact_id, req.certificate_id)

    @router.get("")
    async def list_all(request: Request, limit: int = Query(50, le=200), offset: int = 0):
        return service.list_artifacts(_get_tenant_id(request), limit=limit, offset=offset)

    @router.post("/{artifact_id}/approve")
    async def approve(artifact_id: str, req: ResumeRequest, request: Request):
        return service.resume(_get_tenant_id(request), artifact_id, True, req.approver)

    @router.post("/{artifact_id}/reject")
    async def reject(artifact_id: str, req: ResumeRequest, request: Request):
        return service.resume(_get_tenant_id(request), artifact_id, False, req.approver)

    return router
