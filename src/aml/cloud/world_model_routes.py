"""
src/aml/cloud/world_model_routes.py   (R5)

Factory: create_world_model_router(service) -> APIRouter. Tenant via request.state.tenant.tenant_id.
"""
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from .world_model import (
    WorldModelError, ActionDenied, ActionPendingHITL, ActionInvocation,
)


class RegisterTypeRequest(BaseModel):
    kind: str          # object | link | action
    name: str
    spec: dict

class ValidateObjectRequest(BaseModel):
    type_name: str
    instance: dict

class ValidateLinkRequest(BaseModel):
    link_name: str
    from_type: str
    to_type: str

class InvokeRequest(BaseModel):
    action_name: str
    subject_refs: list
    params: dict = {}
    authority: dict = {}

class ResumeRequest(BaseModel):
    approver: str


def _tenant(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return ctx.tenant_id


def create_world_model_router(service) -> APIRouter:
    router = APIRouter(prefix="/v1/world-model", tags=["World Model"])

    @router.post("/types", status_code=201)
    async def register_type(req: RegisterTypeRequest, request: Request):
        try:
            return service.register_type(_tenant(request), req.kind, req.name, req.spec)
        except WorldModelError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/types")
    async def list_types(request: Request, kind: Optional[str] = Query(None)):
        return service.list_types(_tenant(request), kind=kind)

    @router.get("/types/{type_id}")
    async def get_type(type_id: str, request: Request):
        try:
            return service.get_type(_tenant(request), type_id)
        except WorldModelError:
            raise HTTPException(status_code=404, detail="type not found")

    @router.get("/types/{type_id}/verify")
    async def verify_type(type_id: str, request: Request):
        return service.verify_type(_tenant(request), type_id)

    @router.post("/validate/object")
    async def validate_object(req: ValidateObjectRequest, request: Request):
        try:
            return service.validate_object(_tenant(request), req.type_name, req.instance)
        except WorldModelError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/validate/link")
    async def validate_link(req: ValidateLinkRequest, request: Request):
        try:
            return service.validate_link(_tenant(request), req.link_name, req.from_type, req.to_type)
        except WorldModelError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/actions/invoke", status_code=201)
    async def invoke(req: InvokeRequest, request: Request):
        try:
            return service.invoke_action(_tenant(request), ActionInvocation(**req.model_dump()))
        except ActionDenied as e:
            raise HTTPException(status_code=403, detail={"status": "denied", "reason": e.reason, "action_id": e.action_id})
        except ActionPendingHITL as e:
            raise HTTPException(status_code=202, detail={"status": "waiting_hitl", "action_id": e.action_id})
        except WorldModelError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/actions/stats")
    async def stats(request: Request):
        return service.get_stats(_tenant(request))

    @router.get("/actions/{action_id}")
    async def get_action(action_id: str, request: Request):
        try:
            return service.get_action(_tenant(request), action_id)
        except WorldModelError:
            raise HTTPException(status_code=404, detail="action not found")

    @router.get("/actions/{action_id}/verify")
    async def verify_action(action_id: str, request: Request):
        return service.verify_action(_tenant(request), action_id)

    @router.post("/actions/{action_id}/approve")
    async def approve(action_id: str, req: ResumeRequest, request: Request):
        return service.resume_action(_tenant(request), action_id, True, req.approver)

    @router.post("/actions/{action_id}/reject")
    async def reject(action_id: str, req: ResumeRequest, request: Request):
        return service.resume_action(_tenant(request), action_id, False, req.approver)

    return router
