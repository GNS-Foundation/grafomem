"""
gcrumbs routes — breadcrumb chain + Merkle epoch anchor.

POST /v1/gcrumbs/roll           Seal a new epoch
GET  /v1/gcrumbs/breadcrumbs    List breadcrumbs
GET  /v1/gcrumbs/epochs         List epochs
GET  /v1/gcrumbs/epochs/{n}     Get epoch by number
GET  /v1/gcrumbs/epochs/{n}/proof  Inclusion proof (?seq=N)
GET  /v1/gcrumbs/verify         Verify chain + epochs
GET  /v1/gcrumbs/stats          Stats
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from aml.server.scopes import require_scope

from aml.cloud.gcrumbs import GcrumbsError, GcrumbsService


def _get_tenant_id(request: Request) -> str:
    """Extract tenant_id from auth middleware."""
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        return "default"
    return ctx.tenant_id


def create_gcrumbs_router(svc: GcrumbsService) -> APIRouter:
    router = APIRouter(prefix="/v1/gcrumbs", tags=["gcrumbs"])

    @router.get("/public_key")
    def get_public_key():
        try:
            return {"public_key": svc._pub_hex()}
        except GcrumbsError as e:
            raise HTTPException(503, str(e))

    @router.post("/roll")
    def roll_epoch(request: Request):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "gcrumbs:read")
        try:
            return svc.roll_epoch(tenant_id)
        except GcrumbsError as e:
            raise HTTPException(400, str(e))

    @router.get("/breadcrumbs")
    def list_breadcrumbs(
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "gcrumbs:read")
        return svc.get_breadcrumbs(tenant_id, limit=limit, offset=offset)

    @router.get("/epochs")
    def list_epochs(request: Request):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "gcrumbs:read")
        return svc.get_epochs(tenant_id)

    @router.get("/epochs/{epoch_number}")
    def get_epoch(epoch_number: int, request: Request):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "gcrumbs:read")
        ep = svc.get_epoch(tenant_id, epoch_number)
        if not ep:
            raise HTTPException(404, f"epoch {epoch_number} not found")
        return ep

    @router.get("/epochs/{epoch_number}/proof")
    def inclusion_proof(
        epoch_number: int,
        request: Request,
        seq: int = Query(..., description="breadcrumb seq to prove"),
    ):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "gcrumbs:read")
        try:
            return svc.inclusion_proof(tenant_id, epoch_number, seq)
        except GcrumbsError as e:
            raise HTTPException(404, str(e))

    @router.get("/verify")
    def verify_chain(request: Request):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "gcrumbs:read")
        return svc.verify_chain(tenant_id)

    @router.get("/stats")
    def stats(request: Request):
        tenant_id = _get_tenant_id(request)
        require_scope(request, "gcrumbs:read")
        return svc.get_stats(tenant_id)

    return router
