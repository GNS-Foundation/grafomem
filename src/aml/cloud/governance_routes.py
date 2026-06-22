"""
GRAFOMEM Governance Gateway API — REST endpoints for policy management.

Provides endpoints to create, list, update, delete, and evaluate policies,
plus view evaluation logs. All endpoints are tenant-scoped via API key auth.

Mounted at /v1/governance when Cloud mode is active.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from aml.server.scopes import require_scope

from aml.cloud.schemas import (
    EvaluationResultResponse,
    GovernanceLogResponse,
    GovernanceStatsResponse,
    PolicyListResponse,
    PolicyResponse,
)

logger = logging.getLogger("grafomem.cloud.governance_routes")


# ============================================================================
# Pydantic models
# ============================================================================

class CreatePolicyRequest(BaseModel):
    name: str
    description: str = ""
    policy_type: str  # rate_limit, model_allowlist, content_filter, etc.
    action: str = "deny"  # allow, deny, escalate, log_only
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    priority: int = 100


class UpdatePolicyRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    action: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    priority: int | None = None


class EvaluateRequest(BaseModel):
    """Request body for POST /v1/governance/evaluate."""
    operation: str  # e.g. "write", "retrieve", "inference"
    context: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Helper
# ============================================================================

def _get_tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id

def _get_actor(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        return "system"
    return getattr(ctx, "api_key", "system")

def _audit_logger(request: Request):
    return getattr(request.app.state, "audit_logger", None)


# ============================================================================
# Router factory
# ============================================================================

def create_governance_router(gateway) -> APIRouter:
    """Create the Governance Gateway FastAPI router.

    Parameters
    ----------
    gateway : GovernanceGateway
        The core governance gateway service.
    """
    router = APIRouter(prefix="/v1/governance", tags=["Governance Gateway"])

    # ------------------------------------------------------------------
    # GET /v1/governance/stats — summary stats
    # ------------------------------------------------------------------

    @router.get("/stats", response_model=GovernanceStatsResponse)
    async def governance_stats(request: Request):
        """Summary statistics for governance policies and evaluations."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "governance:read")
        return gateway.get_stats(tenant_id)

    # ------------------------------------------------------------------
    # GET /v1/governance/policy-types — list available types
    # ------------------------------------------------------------------

    @router.get("/policy-types")
    async def list_policy_types():
        """List available policy types and their expected config schemas."""
        return {
            "types": [
                {
                    "type": "rate_limit",
                    "description": "Max operations per time window",
                    "config_schema": {"max_requests": "int", "window_seconds": "int"},
                },
                {
                    "type": "model_allowlist",
                    "description": "Restrict which LLM models can be used",
                    "config_schema": {"models": "list[str]"},
                },
                {
                    "type": "content_filter",
                    "description": "Block queries/outputs matching regex patterns",
                    "config_schema": {"patterns": "list[str]", "check_fields": "list[str]"},
                },
                {
                    "type": "data_scope",
                    "description": "Restrict which stores can be accessed",
                    "config_schema": {"allowed_stores": "list[str]"},
                },
                {
                    "type": "token_budget",
                    "description": "Cap total tokens per request",
                    "config_schema": {"max_tokens_per_request": "int"},
                },
                {
                    "type": "hitl_required",
                    "description": "Require human approval for specified operations",
                    "config_schema": {"operations": "list[str]"},
                },
                {
                    "type": "pii_guard",
                    "description": "Detect PII patterns in model outputs",
                    "config_schema": {"patterns": "list[str]", "check_fields": "list[str]"},
                },
                {
                    "type": "tool_deny",
                    "description": "Deny specific tools from being executed by agents",
                    "config_schema": {"denied_tools": "list[str]"},
                },
            ]
        }

    # ------------------------------------------------------------------
    # POST /v1/governance/policies — create a policy
    # ------------------------------------------------------------------

    @router.post("/policies", response_model=PolicyResponse)
    async def create_policy(req: CreatePolicyRequest, request: Request):
        """Create a new governance policy."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "governance:admin")

        try:
            policy = gateway.create_policy(
                tenant_id=tenant_id,
                name=req.name,
                description=req.description,
                policy_type=req.policy_type,
                action=req.action,
                config=req.config,
                enabled=req.enabled,
                priority=req.priority,
            )
            policy_id = policy.policy_id

            audit = _audit_logger(request)
            if audit:
                audit.log(
                    tenant_id=tenant_id,
                    actor=_get_actor(request),
                    action="create_policy",
                    resource=f"policy:{policy_id}",
                    metadata={"name": req.name, "type": req.policy_type, "action": req.action}
                )

            # Re-fetch for response
            policy = gateway.get_policy(policy_id)
        except Exception as e:
            logger.error("Failed to create policy: %s", e)
            raise HTTPException(500, f"Failed to create policy: {e}")

        return gateway.policy_to_dict(policy)

    # ------------------------------------------------------------------
    # GET /v1/governance/policies — list policies
    # ------------------------------------------------------------------

    @router.get("/policies", response_model=PolicyListResponse)
    async def list_policies(
        request: Request,
        enabled_only: bool = Query(False),
    ):
        """List all governance policies for the tenant."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "governance:read")
        policies = gateway.list_policies(tenant_id, enabled_only=enabled_only)
        return {
            "policies": [gateway.policy_to_dict(p) for p in policies],
            "count": len(policies),
        }

    # ------------------------------------------------------------------
    # GET /v1/governance/policies/{policy_id} — get a policy
    # ------------------------------------------------------------------

    @router.get("/policies/{policy_id}", response_model=PolicyResponse)
    async def get_policy(policy_id: str, request: Request):
        """Get a single governance policy by ID."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "governance:read")
        policy = gateway.get_policy(policy_id)

        if policy is None or policy.tenant_id != tenant_id:
            raise HTTPException(404, f"Policy '{policy_id}' not found")

        return gateway.policy_to_dict(policy)

    # ------------------------------------------------------------------
    # PUT /v1/governance/policies/{policy_id} — update a policy
    # ------------------------------------------------------------------

    @router.put("/policies/{policy_id}", response_model=PolicyResponse)
    async def update_policy(
        policy_id: str, req: UpdatePolicyRequest, request: Request,
    ):
        """Update an existing governance policy."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "governance:admin")

        updated = gateway.update_policy(
            policy_id=policy_id,
            tenant_id=tenant_id,
            name=req.name,
            description=req.description,
            action=req.action,
            config=req.config,
            enabled=req.enabled,
            priority=req.priority,
        )

        if updated is None:
            raise HTTPException(404, f"Policy '{policy_id}' not found")

        audit = _audit_logger(request)
        if audit:
            audit.log(
                tenant_id=tenant_id,
                actor=_get_actor(request),
                action="update_policy",
                resource=f"policy:{policy_id}",
            )

        return gateway.policy_to_dict(updated)

    # ------------------------------------------------------------------
    # DELETE /v1/governance/policies/{policy_id} — delete a policy
    # ------------------------------------------------------------------

    @router.delete("/policies/{policy_id}")
    async def delete_policy(policy_id: str, request: Request):
        """Delete a governance policy."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "governance:admin")
        deleted = gateway.delete_policy(policy_id, tenant_id)

        if not deleted:
            raise HTTPException(404, f"Policy '{policy_id}' not found")

        audit = _audit_logger(request)
        if audit:
            audit.log(
                tenant_id=tenant_id,
                actor=_get_actor(request),
                action="delete_policy",
                resource=f"policy:{policy_id}",
            )

        return {"deleted": True, "policy_id": policy_id}

    # ------------------------------------------------------------------
    # POST /v1/governance/evaluate — evaluate policies
    # ------------------------------------------------------------------

    @router.post("/evaluate")
    async def evaluate_policies(req: EvaluateRequest, request: Request):
        """Evaluate all active policies against a request context.

        Returns the evaluation result for each policy and whether
        the request is allowed overall.
        """
        tenant_id = _get_tenant_id(request)
        require_scope(request, "governance:read")

        allowed, logs = gateway.evaluate_and_gate(
            tenant_id=tenant_id,
            operation=req.operation,
            context=req.context,
        )

        return {
            "allowed": allowed,
            "evaluations": [gateway.log_to_dict(log) for log in logs],
            "summary": {
                "total": len(logs),
                "allowed": sum(1 for l in logs if l.result.value == "allowed"),
                "denied": sum(1 for l in logs if l.result.value == "denied"),
                "escalated": sum(1 for l in logs if l.result.value == "escalated"),
                "logged": sum(1 for l in logs if l.result.value == "logged"),
            },
        }

    # ------------------------------------------------------------------
    # POST /v1/governance/seed-defaults — seed default policies
    # ------------------------------------------------------------------

    @router.post("/seed-defaults")
    async def seed_defaults(request: Request):
        """Create default governance policies for the tenant."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "governance:admin")
        count = gateway.seed_defaults(tenant_id)
        return {"seeded": count}

    # ------------------------------------------------------------------
    # GET /v1/governance/logs — evaluation logs
    # ------------------------------------------------------------------

    @router.get("/logs", response_model=GovernanceLogResponse)
    async def get_evaluation_logs(
        request: Request,
        policy_id: str | None = Query(None),
        result: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        """List policy evaluation logs."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "governance:read")
        logs = gateway.get_logs(
            tenant_id, policy_id=policy_id, result=result,
            limit=limit, offset=offset,
        )
        return {
            "logs": [gateway.log_to_dict(l) for l in logs],
            "count": len(logs),
        }

    return router
