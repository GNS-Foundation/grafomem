"""
GRAFOMEM Memory Sync & Export Routes — Sprint 29.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from aml.server.scopes import require_scope
from pydantic import BaseModel

from aml.cloud.memory_sync import MemorySyncService
from aml.cloud.world_model import WorldModelService
from aml.server.stores import StoreManager
from aml.cloud.audit_export import AuditExportService

def get_memory_sync_routes(
    wm: WorldModelService,
    stores: StoreManager,
    audit: AuditExportService,
) -> APIRouter:
    router = APIRouter(tags=["Memory Sync"])
    sync_service = MemorySyncService(wm, stores, audit)

    @router.get("/{store_id}/export")
    def export_memory_graph(request: Request, store_id: str):
        require_scope(request, "memory:read")
        tenant_id = getattr(request.state, "tenant_id", "default_namespace")
        tenant_role = getattr(request.state, "tenant_role", "admin")
        # Admin check
        if tenant_role not in ("admin", "owner"):
            raise HTTPException(status_code=403, detail="Must be admin to export full memory graph")
        
        try:
            result = sync_service.export_memory(tenant_id, store_id)
            return result
        except KeyError:
            raise HTTPException(status_code=404, detail="Store not found")

    class SyncPayload(BaseModel):
        payload: dict
        metadata: dict

    @router.post("/{store_id}/sync")
    def sync_memory_graph(request: Request, store_id: str, pkg: SyncPayload):
        require_scope(request, "memory:write")
        tenant_id = getattr(request.state, "tenant_id", "default_namespace")
        tenant_role = getattr(request.state, "tenant_role", "admin")
        # Admin check
        if tenant_role not in ("admin", "owner"):
            raise HTTPException(status_code=403, detail="Must be admin to sync memory graph")
        
        try:
            result = sync_service.sync_memory(tenant_id, store_id, pkg.dict())
            return result
        except KeyError:
            raise HTTPException(status_code=404, detail="Store not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return router
