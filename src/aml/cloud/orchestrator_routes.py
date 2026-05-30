"""
GRAFOMEM Agent Orchestrator API — REST endpoints for governed multi-agent execution.

Provides endpoints to define agents, create workflows, execute steps, and
monitor workflow progress.  All endpoints are tenant-scoped via API key auth.

Mounted at /v1/orchestrator when Cloud mode is active.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from aml.cloud.schemas import (
    AgentResponse,
    AgentListResponse,
    WorkflowResponse,
    WorkflowListResponse,
    ReceiptListResponse,
    ChainVerificationResponse,
    OrchestratorStatsResponse,
)

logger = logging.getLogger("grafomem.cloud.orchestrator_routes")


# ============================================================================
# Pydantic models
# ============================================================================

class CreateAgentRequest(BaseModel):
    """Request body for POST /v1/orchestrator/agents."""
    name: str
    role: str = "custom"
    description: str = ""
    model_id: str
    system_prompt: str
    memory_stores: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    max_steps: int = 20
    max_tokens_per_step: int = 4096
    temperature: float = 0.7
    enabled: bool = True


class UpdateAgentRequest(BaseModel):
    """Request body for PUT /v1/orchestrator/agents/{id}."""
    name: str | None = None
    description: str | None = None
    model_id: str | None = None
    system_prompt: str | None = None
    memory_stores: list[str] | None = None
    tools: list[str] | None = None
    max_steps: int | None = None
    max_tokens_per_step: int | None = None
    temperature: float | None = None
    enabled: bool | None = None


class CreateWorkflowRequest(BaseModel):
    """Request body for POST /v1/orchestrator/workflows."""
    name: str
    description: str = ""
    agent_ids: list[str]
    mode: str = "sequential"
    supervisor_agent_id: str | None = None
    max_total_steps: int = 100


class RunWorkflowRequest(BaseModel):
    """Request body for POST /v1/orchestrator/workflows/{id}/run."""
    input_text: str


class ResumeWorkflowRequest(BaseModel):
    """Request body for POST /v1/orchestrator/workflows/{id}/resume."""
    approved: bool


class ExecuteStepRequest(BaseModel):
    """Request body for POST /v1/orchestrator/step."""
    agent_id: str
    input_text: str


# ============================================================================
# Helpers
# ============================================================================

def _get_tenant_id(request: Request) -> str:
    """Extract tenant_id from auth middleware."""
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


# ============================================================================
# Router factory
# ============================================================================

def create_orchestrator_router(orchestrator) -> APIRouter:
    """Create the Agent Orchestrator FastAPI router.

    Parameters
    ----------
    orchestrator : OrchestratorService
        The core orchestrator service.
    """
    router = APIRouter(prefix="/v1/orchestrator", tags=["Agent Orchestrator"])

    # ------------------------------------------------------------------
    # GET /v1/orchestrator/stats
    # ------------------------------------------------------------------

    @router.get("/stats", response_model=OrchestratorStatsResponse)
    async def get_stats(request: Request):
        """Dashboard statistics for the orchestrator."""
        tenant_id = _get_tenant_id(request)
        return orchestrator.get_stats(tenant_id)

    # ------------------------------------------------------------------
    # GET /v1/orchestrator/roles
    # ------------------------------------------------------------------

    @router.get("/roles")
    async def list_roles():
        """List available agent roles."""
        from aml.cloud.orchestrator import AgentRole
        return {
            "roles": [
                {"value": r.value, "label": r.value.replace("_", " ").title()}
                for r in AgentRole
            ]
        }

    # ------------------------------------------------------------------
    # Agent CRUD
    # ------------------------------------------------------------------

    @router.post("/agents")
    async def create_agent(req: CreateAgentRequest, request: Request):
        """Create a new agent definition."""
        tenant_id = _get_tenant_id(request)
        try:
            agent = orchestrator.create_agent(
                tenant_id=tenant_id,
                name=req.name,
                role=req.role,
                model_id=req.model_id,
                system_prompt=req.system_prompt,
                description=req.description,
                memory_stores=req.memory_stores,
                tools=req.tools,
                max_steps=req.max_steps,
                max_tokens_per_step=req.max_tokens_per_step,
                temperature=req.temperature,
                enabled=req.enabled,
            )
            return orchestrator.agent_to_dict(agent)
        except Exception as e:
            logger.error("Failed to create agent: %s", e)
            raise HTTPException(500, f"Failed to create agent: {e}")

    @router.get("/agents")
    async def list_agents(
        request: Request,
        enabled_only: bool = Query(False),
    ):
        """List all agents for the tenant."""
        tenant_id = _get_tenant_id(request)
        agents = orchestrator.list_agents(tenant_id, enabled_only=enabled_only)
        return {
            "agents": [orchestrator.agent_to_dict(a) for a in agents]
        }

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str, request: Request):
        """Get a single agent by ID."""
        tenant_id = _get_tenant_id(request)
        agent = orchestrator.get_agent(agent_id)
        if agent is None or agent.tenant_id != tenant_id:
            raise HTTPException(404, f"Agent '{agent_id}' not found")
        return orchestrator.agent_to_dict(agent)

    @router.put("/agents/{agent_id}")
    async def update_agent(
        agent_id: str,
        req: UpdateAgentRequest,
        request: Request,
    ):
        """Update an agent definition."""
        tenant_id = _get_tenant_id(request)
        updates = req.model_dump(exclude_none=True)

        agent = orchestrator.update_agent(agent_id, tenant_id, **updates)
        if agent is None:
            raise HTTPException(404, f"Agent '{agent_id}' not found")
        return orchestrator.agent_to_dict(agent)

    @router.delete("/agents/{agent_id}")
    async def delete_agent(agent_id: str, request: Request):
        """Delete an agent definition."""
        tenant_id = _get_tenant_id(request)
        deleted = orchestrator.delete_agent(agent_id, tenant_id)
        if not deleted:
            raise HTTPException(404, f"Agent '{agent_id}' not found")
        return {"deleted": True, "agent_id": agent_id}

    # ------------------------------------------------------------------
    # Workflow CRUD
    # ------------------------------------------------------------------

    @router.post("/workflows")
    async def create_workflow(req: CreateWorkflowRequest, request: Request):
        """Create a new workflow."""
        tenant_id = _get_tenant_id(request)
        try:
            workflow = orchestrator.create_workflow(
                tenant_id=tenant_id,
                name=req.name,
                agent_ids=req.agent_ids,
                description=req.description,
                mode=req.mode,
                supervisor_agent_id=req.supervisor_agent_id,
                max_total_steps=req.max_total_steps,
            )
            return orchestrator.workflow_to_dict(workflow)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            logger.error("Failed to create workflow: %s", e)
            raise HTTPException(500, f"Failed to create workflow: {e}")

    @router.get("/workflows")
    async def list_workflows(
        request: Request,
        status: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        """List workflows for the tenant."""
        tenant_id = _get_tenant_id(request)
        workflows = orchestrator.list_workflows(
            tenant_id, status=status, limit=limit, offset=offset,
        )
        return {
            "workflows": [orchestrator.workflow_to_dict(w) for w in workflows]
        }

    @router.get("/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str, request: Request):
        """Get a workflow with all its steps."""
        tenant_id = _get_tenant_id(request)
        workflow = orchestrator.get_workflow(workflow_id)
        if workflow is None or workflow.tenant_id != tenant_id:
            raise HTTPException(404, f"Workflow '{workflow_id}' not found")
        return orchestrator.workflow_to_dict(workflow)

    # ------------------------------------------------------------------
    # Workflow execution
    # ------------------------------------------------------------------

    @router.post("/workflows/{workflow_id}/run")
    async def run_workflow(
        workflow_id: str,
        req: RunWorkflowRequest,
        request: Request,
    ):
        """Start a workflow execution with the given input."""
        tenant_id = _get_tenant_id(request)

        workflow = orchestrator.get_workflow(workflow_id)
        if workflow is None or workflow.tenant_id != tenant_id:
            raise HTTPException(404, f"Workflow '{workflow_id}' not found")

        if workflow.status.value not in ("created", "completed", "terminated"):
            raise HTTPException(
                409,
                f"Workflow is already {workflow.status.value}",
            )

        try:
            result = orchestrator.run_workflow(workflow_id, req.input_text)
            return orchestrator.workflow_to_dict(result)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            logger.error("Workflow execution failed: %s", e)
            raise HTTPException(500, f"Workflow execution failed: {e}")

    @router.post("/workflows/{workflow_id}/stream")
    async def stream_workflow(
        workflow_id: str,
        req: RunWorkflowRequest,
        request: Request,
    ):
        """Stream a workflow execution via Server-Sent Events.

        Returns a text/event-stream response with real-time events as
        each agent step progresses through:
        governance → memory → LLM → tools → complete.

        Event types:
            workflow.started, step.started, step.governance_pass,
            step.governance_deny, step.memory_retrieve, step.llm_start,
            step.llm_complete, step.tool_call, step.complete,
            workflow.complete, workflow.error
        """
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        try:
            from sse_starlette.sse import EventSourceResponse
        except ImportError:
            raise HTTPException(
                501,
                "SSE streaming requires 'sse-starlette' package. "
                "Install with: pip install sse-starlette",
            )

        from aml.cloud.streaming_events import StreamEmitter

        tenant_id = _get_tenant_id(request)

        workflow = orchestrator.get_workflow(workflow_id)
        if workflow is None or workflow.tenant_id != tenant_id:
            raise HTTPException(404, f"Workflow '{workflow_id}' not found")

        if workflow.status.value not in ("created", "completed", "terminated"):
            raise HTTPException(
                409,
                f"Workflow is already {workflow.status.value}",
            )

        loop = asyncio.get_event_loop()
        emitter = StreamEmitter(loop=loop)

        # Run the synchronous orchestrator in a background thread
        executor = ThreadPoolExecutor(max_workers=1)

        def _run_in_thread():
            try:
                orchestrator.run_workflow(
                    workflow_id, req.input_text, emitter=emitter,
                )
            except Exception as e:
                logger.error("Streaming workflow failed: %s", e)
                emitter.emit("workflow.error", {"error": str(e)})
                emitter.close()

        loop.run_in_executor(executor, _run_in_thread)

        async def _event_generator():
            try:
                async for event in emitter.events():
                    yield {
                        "event": event.event,
                        "data": event.to_sse(),
                    }
            except asyncio.CancelledError:
                # Client disconnected
                emitter.close()

        return EventSourceResponse(_event_generator())

    @router.post("/workflows/{workflow_id}/resume")
    async def resume_workflow(
        workflow_id: str,
        req: ResumeWorkflowRequest,
        request: Request,
    ):
        """Resume a workflow waiting for HITL approval."""
        tenant_id = _get_tenant_id(request)

        workflow = orchestrator.get_workflow(workflow_id)
        if workflow is None or workflow.tenant_id != tenant_id:
            raise HTTPException(404, f"Workflow '{workflow_id}' not found")

        try:
            result = orchestrator.resume_workflow(workflow_id, req.approved)
            return orchestrator.workflow_to_dict(result)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.post("/workflows/{workflow_id}/terminate")
    async def terminate_workflow(workflow_id: str, request: Request):
        """Force-terminate a running workflow."""
        tenant_id = _get_tenant_id(request)
        success = orchestrator.terminate_workflow(workflow_id, tenant_id)
        if not success:
            raise HTTPException(404, f"Workflow '{workflow_id}' not found")
        return {"terminated": True, "workflow_id": workflow_id}

    # ------------------------------------------------------------------
    # Ad-hoc step
    # ------------------------------------------------------------------

    @router.post("/step")
    async def execute_step(req: ExecuteStepRequest, request: Request):
        """Execute a single ad-hoc step (not part of a workflow).

        Creates a temporary workflow and executes one governed step.
        Useful for testing agents and one-shot queries.
        """
        tenant_id = _get_tenant_id(request)
        try:
            step = orchestrator.execute_adhoc_step(
                tenant_id=tenant_id,
                agent_id=req.agent_id,
                input_text=req.input_text,
            )
            return orchestrator.step_to_dict(step)
        except ValueError as e:
            raise HTTPException(404, str(e))
        except Exception as e:
            logger.error("Ad-hoc step failed: %s", e)
            raise HTTPException(500, f"Step execution failed: {e}")

    # ------------------------------------------------------------------
    # Step detail
    # ------------------------------------------------------------------

    @router.get("/steps/{step_id}")
    async def get_step(step_id: str, request: Request):
        """Get a single step by ID."""
        tenant_id = _get_tenant_id(request)

        conn = orchestrator._get_conn()
        row = conn.execute(
            "SELECT * FROM orchestrator_steps "
            "WHERE step_id = %s AND tenant_id = %s",
            (step_id, tenant_id),
        ).fetchone()

        if row is None:
            raise HTTPException(404, f"Step '{step_id}' not found")

        step = orchestrator._row_to_step(row)
        return orchestrator.step_to_dict(step)

    # ------------------------------------------------------------------
    # Execution Receipts — hash-chained attestation (Sprint 7b)
    # ------------------------------------------------------------------

    @router.get("/workflows/{workflow_id}/receipts")
    async def get_workflow_receipts(workflow_id: str, request: Request):
        """Get all execution receipts for a workflow."""
        tenant_id = _get_tenant_id(request)
        receipt_svc = getattr(request.app.state, "execution_receipts", None)
        if not receipt_svc:
            raise HTTPException(501, "Execution Receipt Service not available")

        workflow = orchestrator.get_workflow(workflow_id)
        if workflow is None or workflow.tenant_id != tenant_id:
            raise HTTPException(404, f"Workflow '{workflow_id}' not found")

        receipts = receipt_svc.get_receipts(workflow_id)
        return {
            "workflow_id": workflow_id,
            "receipts": [receipt_svc.receipt_to_dict(r) for r in receipts],
            "count": len(receipts),
        }

    @router.get("/workflows/{workflow_id}/verify-chain", response_model=ChainVerificationResponse)
    async def verify_receipt_chain(workflow_id: str, request: Request):
        """Verify the hash chain integrity of a workflow's receipts."""
        tenant_id = _get_tenant_id(request)
        receipt_svc = getattr(request.app.state, "execution_receipts", None)
        if not receipt_svc:
            raise HTTPException(501, "Execution Receipt Service not available")

        workflow = orchestrator.get_workflow(workflow_id)
        if workflow is None or workflow.tenant_id != tenant_id:
            raise HTTPException(404, f"Workflow '{workflow_id}' not found")

        verdict = receipt_svc.verify_chain(workflow_id)
        return receipt_svc.verdict_to_dict(verdict)

    @router.get("/receipts/{receipt_id}")
    async def get_receipt(receipt_id: str, request: Request):
        """Get a single execution receipt."""
        tenant_id = _get_tenant_id(request)
        receipt_svc = getattr(request.app.state, "execution_receipts", None)
        if not receipt_svc:
            raise HTTPException(501, "Execution Receipt Service not available")

        receipt = receipt_svc.get_receipt(receipt_id)
        if receipt is None or receipt.tenant_id != tenant_id:
            raise HTTPException(404, f"Receipt '{receipt_id}' not found")

        return receipt_svc.receipt_to_dict(receipt)

    # ------------------------------------------------------------------
    # Deterministic Replay (Sprint 7d)
    # ------------------------------------------------------------------

    @router.post("/replay/{decision_id}")
    async def replay_decision(decision_id: str, request: Request):
        """Re-execute a decision with frozen inputs and verify output.

        WARNING: This calls the LLM provider and incurs costs.
        """
        tenant_id = _get_tenant_id(request)
        replay_svc = getattr(request.app.state, "replay_engine", None)
        if not replay_svc:
            raise HTTPException(501, "Replay Engine not available")

        try:
            verdict = replay_svc.replay(decision_id, tenant_id)
            return replay_svc.verdict_to_dict(verdict)
        except Exception as e:
            logger.error("Replay failed: %s", e)
            raise HTTPException(500, f"Replay failed: {e}")

    return router
