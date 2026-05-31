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

from fastapi import APIRouter, HTTPException, Query

from aml.cloud.gcrumbs import GcrumbsError, GcrumbsService


def create_gcrumbs_router(svc: GcrumbsService) -> APIRouter:
    router = APIRouter(prefix="/v1/gcrumbs", tags=["gcrumbs"])

    @router.post("/roll")
    def roll_epoch(tenant_id: str = Query("default")):
        try:
            return svc.roll_epoch(tenant_id)
        except GcrumbsError as e:
            raise HTTPException(400, str(e))

    @router.get("/breadcrumbs")
    def list_breadcrumbs(
        tenant_id: str = Query("default"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        return svc.get_breadcrumbs(tenant_id, limit=limit, offset=offset)

    @router.get("/epochs")
    def list_epochs(tenant_id: str = Query("default")):
        return svc.get_epochs(tenant_id)

    @router.get("/epochs/{epoch_number}")
    def get_epoch(epoch_number: int, tenant_id: str = Query("default")):
        ep = svc.get_epoch(tenant_id, epoch_number)
        if not ep:
            raise HTTPException(404, f"epoch {epoch_number} not found")
        return ep

    @router.get("/epochs/{epoch_number}/proof")
    def inclusion_proof(
        epoch_number: int,
        seq: int = Query(..., description="breadcrumb seq to prove"),
        tenant_id: str = Query("default"),
    ):
        try:
            return svc.inclusion_proof(tenant_id, epoch_number, seq)
        except GcrumbsError as e:
            raise HTTPException(404, str(e))

    @router.get("/verify")
    def verify_chain(tenant_id: str = Query("default")):
        return svc.verify_chain(tenant_id)

    @router.get("/stats")
    def stats(tenant_id: str = Query("default")):
        return svc.get_stats(tenant_id)

    return router
