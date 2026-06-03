"""
GRAFOMEM LLM & Tool Management API — REST endpoints for BYOM and tool registry.

Provides endpoints to register LLM providers, register custom tools,
and manage the configuration. Mounted at /v1/llm when Cloud mode is active.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from aml.cloud.schemas import (
    LLMProviderResponse,
    LLMProviderListResponse,
    ToolListResponse,
)

logger = logging.getLogger("grafomem.cloud.llm_routes")


# ============================================================================
# Pydantic models
# ============================================================================

class RegisterProviderRequest(BaseModel):
    """Request body for POST /v1/llm/providers."""
    provider: str  # openai, anthropic, gemini, ollama, custom
    model_id: str
    api_key: str | None = None
    base_url: str | None = None
    default_temperature: float = 0.7
    max_tokens: int = 4096
    enabled: bool = True


class RegisterToolRequest(BaseModel):
    """Request body for POST /v1/llm/tools."""
    name: str
    description: str
    tool_type: str  # memory_read, memory_write, etc.
    input_schema: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    requires_governance: bool = True


# ============================================================================
# Helpers
# ============================================================================

def _get_tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


# ============================================================================
# Router factory
# ============================================================================

def create_llm_router(llm_registry, tool_registry) -> APIRouter:
    """Create the LLM & Tool Management FastAPI router.

    Parameters
    ----------
    llm_registry : LLMRegistry
        The LLM provider registry.
    tool_registry : ToolRegistry
        The tool definition registry.
    """
    router = APIRouter(prefix="/v1/llm", tags=["LLM & Tools"])

    # ------------------------------------------------------------------
    # Provider management
    # ------------------------------------------------------------------

    @router.post("/providers", response_model=LLMProviderResponse)
    async def register_provider(req: RegisterProviderRequest, request: Request):
        """Register or update an LLM provider for your tenant."""
        tenant_id = _get_tenant_id(request)
        try:
            config = llm_registry.register_provider(
                tenant_id=tenant_id,
                provider=req.provider,
                model_id=req.model_id,
                api_key=req.api_key,
                base_url=req.base_url,
                default_temperature=req.default_temperature,
                max_tokens=req.max_tokens,
                enabled=req.enabled,
            )
            return llm_registry.config_to_dict(config)
        except Exception as e:
            logger.error("Failed to register provider: %s", e)
            raise HTTPException(500, f"Failed to register provider: {e}")

    @router.get("/providers", response_model=LLMProviderListResponse)
    async def list_providers(request: Request):
        """List all registered LLM providers for your tenant."""
        tenant_id = _get_tenant_id(request)
        providers = llm_registry.list_providers(tenant_id)
        return {
            "providers": [llm_registry.config_to_dict(p) for p in providers],
            "count": len(providers),
        }

    @router.delete("/providers/{model_id}")
    async def delete_provider(model_id: str, request: Request):
        """Remove an LLM provider registration."""
        tenant_id = _get_tenant_id(request)
        deleted = llm_registry.delete_provider(tenant_id, model_id)
        if not deleted:
            raise HTTPException(404, f"Provider '{model_id}' not found")
        return {"deleted": True, "model_id": model_id}

    @router.patch("/providers/{model_id}", response_model=LLMProviderResponse)
    async def update_provider(model_id: str, request: Request):
        """Update an existing LLM provider (API key, base URL, temperature, max tokens)."""
        tenant_id = _get_tenant_id(request)
        body = await request.json()

        # Find existing provider
        existing = llm_registry.get_provider(tenant_id, model_id)
        if existing is None:
            raise HTTPException(404, f"Provider '{model_id}' not found")

        try:
            # Strip whitespace/newlines from API keys (prevents httpx header errors)
            new_key = body.get("api_key")
            if isinstance(new_key, str):
                new_key = new_key.strip()

            config = llm_registry.register_provider(
                tenant_id=tenant_id,
                provider=existing.provider.value,
                model_id=model_id,
                api_key=new_key or existing.api_key,
                base_url=body.get("base_url", existing.base_url),
                default_temperature=body.get("default_temperature", existing.default_temperature),
                max_tokens=body.get("max_tokens", existing.max_tokens),
                enabled=body.get("enabled", existing.enabled),
            )
            return llm_registry.config_to_dict(config)
        except Exception as e:
            logger.error("Failed to update provider: %s", e)
            raise HTTPException(500, f"Failed to update provider: {e}")

    # ------------------------------------------------------------------
    # Tool management
    # ------------------------------------------------------------------

    @router.post("/tools")
    async def register_tool(req: RegisterToolRequest, request: Request):
        """Register a custom tool for your tenant."""
        tenant_id = _get_tenant_id(request)
        try:
            tool = tool_registry.register_tool(
                tenant_id=tenant_id,
                name=req.name,
                description=req.description,
                tool_type=req.tool_type,
                input_schema=req.input_schema,
                config=req.config,
                enabled=req.enabled,
                requires_governance=req.requires_governance,
            )
            return tool_registry.tool_to_dict(tool)
        except Exception as e:
            logger.error("Failed to register tool: %s", e)
            raise HTTPException(500, f"Failed to register tool: {e}")

    @router.get("/tools", response_model=ToolListResponse)
    async def list_tools(request: Request):
        """List all tools (built-in + custom) for your tenant."""
        tenant_id = _get_tenant_id(request)
        tools = tool_registry.list_tools(tenant_id)
        return {
            "tools": [tool_registry.tool_to_dict(t) for t in tools]
        }

    @router.delete("/tools/{tool_name}")
    async def delete_tool(tool_name: str, request: Request):
        """Remove a custom tool (built-in tools cannot be deleted)."""
        tenant_id = _get_tenant_id(request)
        deleted = tool_registry.delete_tool(tenant_id, tool_name)
        if not deleted:
            raise HTTPException(
                404,
                f"Tool '{tool_name}' not found or is a built-in tool",
            )
        return {"deleted": True, "tool_name": tool_name}

    # ------------------------------------------------------------------
    # Seed built-in tools
    # ------------------------------------------------------------------

    @router.post("/tools/seed-builtins")
    async def seed_builtins(request: Request):
        """Seed built-in tools for your tenant (grafomem_retrieve, etc.)."""
        tenant_id = _get_tenant_id(request)
        count = tool_registry.seed_builtin_tools(tenant_id)
        return {"seeded": count}

    return router
