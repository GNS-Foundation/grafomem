"""CGR scoring HTTP surface (read-only). Guarded by the `cgr:read` scope.

  GET /v1/cgr/scores              → {scores: [CGRResult...], as_of, dimension}
  GET /v1/cgr/scores/{handle}     → {score: CGRResult, tiergate: {...}}

Thin web adapter: the only cgr module that imports fastapi + the scope helper.
All scoring lives in aml.cgr.engine; dependencies (decision_trail, store_manager)
are injected by the app factory.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from aml.cgr.engine import compute_scores, to_tiergate
from aml.cgr.scoring import DIMENSION_RECEIVABLES, _now_iso


def _tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


def create_cgr_scoring_router(decision_trail, store_manager) -> APIRouter:
    from aml.server.scopes import require_scope

    router = APIRouter(prefix="/v1/cgr", tags=["CGR Scores"])

    def _guard():
        if decision_trail is None or store_manager is None:
            raise HTTPException(503, "CGR scoring services not available")

    @router.get("/scores")
    async def list_scores(request: Request, limit: int = 500, offset: int = 0):
        tenant_id = _tenant_id(request)
        require_scope(request, "cgr:read")
        _guard()
        results = compute_scores(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
        return {
            "scores": [asdict(r) for r in results],
            "count": len(results),
            "as_of": results[0].as_of if results else _now_iso(),
            "dimension": DIMENSION_RECEIVABLES,
        }

    @router.get("/scores/{agent_handle:path}")
    async def get_score(agent_handle: str, request: Request, limit: int = 500, offset: int = 0):
        tenant_id = _tenant_id(request)
        require_scope(request, "cgr:read")
        _guard()
        results = compute_scores(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
        match = next((r for r in results if r.agent_handle == agent_handle), None)
        if match is None:
            raise HTTPException(404, f"No CGR score for agent_handle {agent_handle!r}")
        return {"score": asdict(match), "tiergate": to_tiergate(match)}

    return router
