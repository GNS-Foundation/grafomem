import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from aml.server.scopes import require_scope
from pydantic import BaseModel

from aml.cloud.world_model import WorldModelService
from aml.cloud.templates.engine import TemplateEngine
from aml.cloud.templates import registry

logger = logging.getLogger("grafomem.cloud.templates")

def _get_tenant_id(request: Request) -> str:
    """Caller's tenant from the auth context. Same helper shape as the artifact/landing
    routers — the install writes ONLY to this tenant; there is no tenant parameter, so a
    caller can never target another tenant's World Model."""
    ctx = getattr(request.state, "tenant", None)
    if ctx is None or not getattr(ctx, "tenant_id", None):
        raise HTTPException(status_code=401, detail="not authenticated")
    return ctx.tenant_id


def get_template_routes(world_model: WorldModelService) -> APIRouter:
    router = APIRouter(tags=["Templates"])
    engine = TemplateEngine(world_model)

    class InstallTemplateRequest(BaseModel):
        template_id: str

    @router.get("/")
    def list_templates(request: Request):
        """List all available canonical templates."""
        require_scope(request, "manifold:read")
        return {"templates": registry.list_templates()}

    @router.post("/install")
    def install_template(req: InstallTemplateRequest, request: Request):
        """Install a template into the CALLER'S World Model. tenant_id is derived from
        the auth context (never a hardcoded/target value), so the governed install is
        tenant-scoped: a caller can only seed its own tenant."""
        require_scope(request, "artifacts:admin")
        tenant_id = _get_tenant_id(request)

        try:
            yaml_content = registry.get_template(req.template_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
            
        try:
            result = engine.install_template(tenant_id, yaml_content)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.error("Template installation failed: %s", e)
            raise HTTPException(status_code=400, detail=f"Installation failed: {str(e)}")

    return router
