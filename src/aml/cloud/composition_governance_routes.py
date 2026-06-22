"""
src/aml/cloud/composition_governance_routes.py   (R4)

Factory: create_composition_governance_router(service) -> APIRouter. Tenant via request.state.tenant.tenant_id.
"""
from fastapi import APIRouter, Request, HTTPException, Query
from aml.server.scopes import require_scope
from pydantic import BaseModel

from .composition_governance import (
    CompositionError, ComposeRejected, ComposePendingHITL, ComposeRequest,
)


class ComposeBody(BaseModel):
    composition_kind: str
    members: list                  # [{ref_id, license, certified}]
    target_ref: str
    authority: dict = {}
    required_trust_tier: str = "verified"

class ResumeBody(BaseModel):
    approver: str


def _tenant(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return ctx.tenant_id


def create_composition_governance_router(service) -> APIRouter:
    router = APIRouter(prefix="/v1/compositions", tags=["Composition Governance"])

    @router.post("", status_code=201)
    async def compose(body: ComposeBody, request: Request):
        require_scope(request, "artifacts:admin")
        try:
            return service.compose(_tenant(request), ComposeRequest(**body.model_dump()))
        except ComposeRejected as e:
            raise HTTPException(status_code=422, detail={"status": "rejected", "reasons": e.reasons})
        except ComposePendingHITL as e:
            raise HTTPException(status_code=202, detail={"status": "waiting_hitl", "composition_id": e.composition_id})
        except CompositionError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/stats")
    async def stats(request: Request):
        require_scope(request, "artifacts:read")
        return service.get_stats(_tenant(request))

    @router.get("")
    async def list_all(request: Request, limit: int = Query(50, le=200), offset: int = 0):
        require_scope(request, "artifacts:read")
        return service.list_compositions(_tenant(request), limit=limit, offset=offset)

    @router.get("/{composition_id}")
    async def get_one(composition_id: str, request: Request):
        require_scope(request, "artifacts:read")
        try:
            return service.get(_tenant(request), composition_id)
        except CompositionError:
            raise HTTPException(status_code=404, detail="composition not found")

    @router.get("/{composition_id}/verify")
    async def verify(composition_id: str, request: Request):
        require_scope(request, "artifacts:read")
        return service.verify(_tenant(request), composition_id)

    @router.get("/{composition_id}/artifact")
    async def composed_artifact(composition_id: str, request: Request):
        """Descriptor to register the composed result back in R1."""
        require_scope(request, "artifacts:read")
        return service.composed_artifact(_tenant(request), composition_id)

    @router.post("/{composition_id}/approve")
    async def approve(composition_id: str, body: ResumeBody, request: Request):
        require_scope(request, "artifacts:admin")
        return service.resume(_tenant(request), composition_id, True, body.approver)

    @router.post("/{composition_id}/reject")
    async def reject(composition_id: str, body: ResumeBody, request: Request):
        require_scope(request, "artifacts:admin")
        return service.resume(_tenant(request), composition_id, False, body.approver)

    return router
