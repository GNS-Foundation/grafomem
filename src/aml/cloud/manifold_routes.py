from fastapi import APIRouter, Request
from aml.cloud.manifold import ManifoldService

def create_manifold_router(manifold_svc: ManifoldService) -> APIRouter:
    router = APIRouter()

    @router.get("/export")
    async def export_manifold(request: Request):
        ctx = getattr(request.state, "tenant", None)
        tenant = ctx.tenant_id if ctx else "default"
        
        # This executes synchronously (blocking) for now per Sprint 30 design.
        # MiniSom and Pandas read_sql takes a few seconds. 
        # Future optimization: background worker + redis/postgres cache.
        try:
            manifold_data = manifold_svc.generate_manifold(tenant)
            return manifold_data
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=str(e))

    return router
