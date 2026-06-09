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

    @router.get("/locate/{step_id}")
    async def locate_manifold_step(step_id: str, request: Request):
        ctx = getattr(request.state, "tenant", None)
        tenant = ctx.tenant_id if ctx else "default"
        try:
            res = manifold_svc.locate_step(step_id, tenant)
            if "error" in res:
                from fastapi import HTTPException
                raise HTTPException(status_code=400, detail=res["error"])
            return res
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/clear_cache")
    async def clear_cache(request: Request):
        ctx = getattr(request.state, "tenant", None)
        tenant = ctx.tenant_id if ctx else "default"
        try:
            import psycopg2
            conn = psycopg2.connect(manifold_svc.db_url)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM manifold_cache WHERE tenant_id = %s", (tenant,))
            conn.commit()
            conn.close()
            return {"status": "ok"}
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=str(e))

    return router
