"""
GRAFOMEM Decision Trail API — REST endpoints for inference audit logging.

Provides endpoints to log, query, replay, and export AI inference decisions.
All endpoints are tenant-scoped via API key authentication.

Mounted at /v1/decisions when Cloud mode is active.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aml.server.scopes import require_scope

logger = logging.getLogger("grafomem.cloud.decision_routes")


# ============================================================================
# Pydantic models
# ============================================================================

class LogDecisionRequest(BaseModel):
    """Request body for POST /v1/decisions/log."""
    store_id: str
    session_id: str | None = None
    query: str
    retrieved_fact_refs: list[int] = Field(default_factory=list)
    retrieved_contents: list[str] = Field(default_factory=list)
    retrieval_scores: list[float] = Field(default_factory=list)
    retrieval_options: dict[str, Any] = Field(default_factory=dict)
    model_id: str
    prompt_template_hash: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    raw_output: str
    parsed_output: dict[str, Any] | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    signing_key: str | None = None  # hex-encoded Ed25519 private seed
    parent_decision_id: str | None = None


class DecisionResponse(BaseModel):
    """Response model for a single decision record."""
    decision_id: str
    tenant_id: str
    store_id: str
    session_id: str | None = None
    created_at: str  # ISO-8601
    query: str
    retrieved_fact_refs: list[int] = Field(default_factory=list)
    retrieved_contents: list[str] = Field(default_factory=list)
    retrieval_scores: list[float] = Field(default_factory=list)
    retrieval_options: dict[str, Any] = Field(default_factory=dict)
    model_id: str
    prompt_hash: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    raw_output: str
    parsed_output: dict[str, Any] | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    signature: str | None = None  # base64
    public_key: str | None = None  # base64
    parent_decision_id: str | None = None


class ReplayResponse(BaseModel):
    """Response model for decision replay."""
    decision: DecisionResponse
    memory_state_at_decision: list[dict[str, Any]] = Field(default_factory=list)
    facts_used: list[dict[str, Any]] = Field(default_factory=list)
    facts_since_deleted: list[int] = Field(default_factory=list)


class ScrubResponse(BaseModel):
    """Response for GDPR fact scrubbing."""
    fact_ref: int
    decisions_affected: int


# ============================================================================
# Helper
# ============================================================================

def _record_to_response(rec) -> DecisionResponse:
    """Convert a DecisionRecord to a DecisionResponse."""
    return DecisionResponse(
        decision_id=rec.decision_id,
        tenant_id=rec.tenant_id,
        store_id=rec.store_id,
        session_id=rec.session_id,
        created_at=rec.created_at.isoformat(),
        query=rec.query,
        retrieved_fact_refs=rec.retrieved_refs,
        retrieved_contents=rec.retrieved_contents,
        retrieval_scores=rec.retrieval_scores,
        retrieval_options=rec.retrieval_options,
        model_id=rec.model_id,
        prompt_hash=rec.prompt_hash,
        parameters=rec.parameters,
        raw_output=rec.raw_output,
        parsed_output=rec.parsed_output,
        output_tokens=rec.output_tokens,
        latency_ms=rec.latency_ms,
        signature=base64.b64encode(rec.signature).decode() if rec.signature else None,
        public_key=base64.b64encode(rec.public_key).decode() if rec.public_key else None,
        parent_decision_id=rec.parent_decision_id,
    )


def _get_tenant_id(request: Request) -> str:
    """Extract tenant_id from auth middleware. Raises 401 if not authenticated."""
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


# ============================================================================
# Router factory
# ============================================================================

def create_decision_router(decision_trail, store_manager=None, tenant_auth=None) -> APIRouter:
    """Create the Decision Trail FastAPI router.

    Parameters
    ----------
    decision_trail : DecisionTrailService
        The core decision trail service.
    store_manager : StoreManager, optional
        For replay — to query memory state at decision time.
    tenant_auth : PortalAuth, optional
        Not used directly; auth comes from middleware.
    """
    router = APIRouter(prefix="/v1/decisions", tags=["Decision Trail"])

    # ------------------------------------------------------------------
    # POST /v1/decisions/log — log a decision
    # ------------------------------------------------------------------

    @router.post("/log", response_model=DecisionResponse)
    async def log_decision(req: LogDecisionRequest, request: Request):
        """Log an AI inference decision with full provenance.

        Records the query, retrieved facts, model used, output produced,
        and optionally Ed25519-signs the decision record.
        """
        tenant_id = _get_tenant_id(request)
        require_scope(request, "decisions:read")

        # Removed client-provided signing_key parsing (Phase 0 KMS compliance)

        try:
            record = decision_trail.log(
                tenant_id=tenant_id,
                store_id=req.store_id,
                query=req.query,
                model_id=req.model_id,
                raw_output=req.raw_output,
                session_id=req.session_id,
                retrieved_refs=req.retrieved_fact_refs,
                retrieved_contents=req.retrieved_contents,
                retrieval_scores=req.retrieval_scores,
                retrieval_options=req.retrieval_options,
                prompt_hash=req.prompt_template_hash,
                parameters=req.parameters,
                parsed_output=req.parsed_output,
                output_tokens=req.output_tokens,
                latency_ms=req.latency_ms,
                signing_identity=request.app.state.signing_identity,
                parent_decision_id=req.parent_decision_id,
            )
        except Exception as e:
            logger.error("Failed to log decision: %s", e)
            raise HTTPException(500, f"Failed to log decision: {e}")

        return _record_to_response(record)

    # ------------------------------------------------------------------
    # GET /v1/decisions/stats — summary stats (must be before /{decision_id})
    # ------------------------------------------------------------------

    @router.get("/stats")
    async def decision_stats(request: Request):
        """Summary statistics for the tenant's decision trail."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "decisions:read")
        return decision_trail.get_stats(tenant_id)

    # ------------------------------------------------------------------
    # GET /v1/decisions/export — bulk JSON export (must be before /{decision_id})
    # ------------------------------------------------------------------

    @router.get("/export")
    async def export_decisions(
        request: Request,
        from_time: datetime | None = Query(None, alias="from"),
        to_time: datetime | None = Query(None, alias="to"),
    ):
        """Bulk export all decisions as newline-delimited JSON (NDJSON).

        Streams the response for memory efficiency with large datasets.
        Suitable for feeding to auditors, compliance tools, or SIEM systems.
        """
        tenant_id = _get_tenant_id(request)
        require_scope(request, "decisions:read")

        def generate():
            for record_dict in decision_trail.export(
                tenant_id, from_time=from_time, to_time=to_time,
            ):
                yield json.dumps(record_dict) + "\n"

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f"attachment; filename=decisions_{tenant_id}.ndjson",
            },
        )

    # ------------------------------------------------------------------
    # GET /v1/decisions/{decision_id} — get a single decision
    # ------------------------------------------------------------------

    @router.get("/{decision_id}", response_model=DecisionResponse)
    async def get_decision(decision_id: str, request: Request):
        """Retrieve a single decision record by its ID."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "decisions:read")
        record = decision_trail.get(decision_id)

        if record is None:
            raise HTTPException(404, f"Decision '{decision_id}' not found")
        if record.tenant_id != tenant_id:
            raise HTTPException(404, f"Decision '{decision_id}' not found")

        return _record_to_response(record)

    # ------------------------------------------------------------------
    # GET /v1/decisions/{decision_id}/replay — replay a decision
    # ------------------------------------------------------------------

    @router.get("/{decision_id}/replay", response_model=ReplayResponse)
    async def replay_decision(decision_id: str, request: Request):
        """Replay a decision: show the decision, the memory state at that
        time, which facts were used, and which have been deleted since.

        This is the core auditability feature: reconstruct exactly what
        the AI knew when it made a decision.
        """
        tenant_id = _get_tenant_id(request)
        require_scope(request, "decisions:read")
        record = decision_trail.get(decision_id)

        if record is None:
            raise HTTPException(404, f"Decision '{decision_id}' not found")
        if record.tenant_id != tenant_id:
            raise HTTPException(404, f"Decision '{decision_id}' not found")

        decision_resp = _record_to_response(record)

        # Try to reconstruct memory state at decision time
        memory_state = []
        facts_used = []
        facts_since_deleted = []

        if store_manager is not None:
            entry = store_manager.get(record.store_id)
            if entry is not None:
                backend = entry.backend

                # Get all memories via audit to check which refs still exist
                try:
                    all_memories = list(backend.audit())
                    current_refs = {m.ref for m in all_memories}

                    # Convert memories to dicts for the response
                    for m in all_memories:
                        memory_state.append({
                            "ref": m.ref,
                            "content": m.content,
                            "written_at": m.written_at.isoformat() if m.written_at else None,
                            "tenant_id": m.tenant_id,
                            "valid_from": m.valid_from.isoformat() if m.valid_from else None,
                            "valid_until": m.valid_until.isoformat() if m.valid_until else None,
                        })

                    # Identify which retrieved facts still exist vs deleted
                    for i, ref in enumerate(record.retrieved_refs):
                        fact_info = {
                            "ref": ref,
                            "content": record.retrieved_contents[i] if i < len(record.retrieved_contents) else None,
                            "score": record.retrieval_scores[i] if i < len(record.retrieval_scores) else None,
                        }
                        facts_used.append(fact_info)

                        if ref not in current_refs:
                            facts_since_deleted.append(ref)

                except Exception as e:
                    logger.warning("Could not replay memory state: %s", e)

        return ReplayResponse(
            decision=decision_resp,
            memory_state_at_decision=memory_state,
            facts_used=facts_used,
            facts_since_deleted=facts_since_deleted,
        )

    # ------------------------------------------------------------------
    # GET /v1/decisions/ — query decisions
    # ------------------------------------------------------------------

    @router.get("/")
    async def query_decisions(
        request: Request,
        store_id: str | None = Query(None),
        session_id: str | None = Query(None),
        model_id: str | None = Query(None),
        from_time: datetime | None = Query(None, alias="from"),
        to_time: datetime | None = Query(None, alias="to"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        """Query decisions with filters and pagination."""
        tenant_id = _get_tenant_id(request)
        require_scope(request, "decisions:read")

        records = decision_trail.query_decisions(
            tenant_id,
            from_time=from_time,
            to_time=to_time,
            store_id=store_id,
            session_id=session_id,
            model_id=model_id,
            limit=limit,
            offset=offset,
        )

        return {
            "decisions": [_record_to_response(r) for r in records],
            "count": len(records),
            "limit": limit,
            "offset": offset,
        }

    # ------------------------------------------------------------------
    # DELETE /v1/decisions/scrub/{fact_ref} — GDPR scrub
    # ------------------------------------------------------------------

    @router.delete("/scrub/{fact_ref}", response_model=ScrubResponse)
    async def scrub_fact(fact_ref: int, request: Request):
        """Scrub a deleted fact from all decision records (GDPR Article 17).

        When a fact is hard-deleted from memory, call this endpoint to
        replace its content in all decision records with '[REDACTED]'.
        Returns the number of affected decisions.
        """
        tenant_id = _get_tenant_id(request)
        require_scope(request, "decisions:read")

        try:
            affected = decision_trail.scrub_fact(fact_ref, tenant_id)
        except Exception as e:
            logger.error("GDPR scrub failed: %s", e)
            raise HTTPException(500, f"Scrub failed: {e}")

        return ScrubResponse(fact_ref=fact_ref, decisions_affected=affected)

    return router
