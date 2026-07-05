from fastapi import APIRouter, Request
from aml.cloud.manifold import ManifoldService
from aml.server.scopes import require_scope

def create_manifold_router(manifold_svc: ManifoldService) -> APIRouter:
    router = APIRouter()

    @router.get("/export")
    async def export_manifold(request: Request):
        ctx = getattr(request.state, "tenant", None)
        tenant = ctx.tenant_id if ctx else "default"
        require_scope(request, "manifold:read")
        
        # This executes synchronously (blocking) for now per Sprint 30 design.
        # MiniSom and Pandas read_sql takes a few seconds. 
        # Future optimization: background worker + redis/postgres cache.
        try:
            manifold_data = manifold_svc.generate_manifold(tenant)
            return manifold_data
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("Manifold export error")
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=f"Internal server error: {repr(e)}")

    @router.get("/locate/{step_id}")
    async def locate_manifold_step(step_id: str, request: Request):
        ctx = getattr(request.state, "tenant", None)
        tenant = ctx.tenant_id if ctx else "default"
        require_scope(request, "manifold:read")
        try:
            res = manifold_svc.locate_step(step_id, tenant)
            if "error" in res:
                from fastapi import HTTPException
                raise HTTPException(status_code=400, detail=res["error"])
            return res
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("Manifold locate error")
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail="Internal server error")

    @router.post("/clear_cache")
    async def clear_cache(request: Request):
        ctx = getattr(request.state, "tenant", None)
        tenant = ctx.tenant_id if ctx else "default"
        require_scope(request, "manifold:read")
        try:
            import psycopg2
            conn = psycopg2.connect(manifold_svc.db_url)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM manifold_cache WHERE tenant_id = %s", (tenant,))
            conn.commit()
            conn.close()
            return {"status": "ok"}
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("Manifold cache error")
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail="Internal server error")

    return router
