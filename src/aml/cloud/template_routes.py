import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aml.cloud.world_model import WorldModelService
from aml.cloud.templates.engine import TemplateEngine
from aml.cloud.templates import registry

logger = logging.getLogger("grafomem.cloud.templates")

def get_template_routes(world_model: WorldModelService) -> APIRouter:
    router = APIRouter(tags=["Templates"])
    engine = TemplateEngine(world_model)

    class InstallTemplateRequest(BaseModel):
        template_id: str

    @router.get("/")
    def list_templates():
        """List all available canonical templates."""
        return {"templates": registry.list_templates()}

    @router.post("/install")
    def install_template(req: InstallTemplateRequest):
        """Install a template into the tenant's World Model."""
        # For simplicity, using a hardcoded tenant for now, 
        # normally extracted from auth context.
        tenant_id = "tenant_001" 
        
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
