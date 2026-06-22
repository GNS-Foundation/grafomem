"""
src/aml/cloud/provenance_customs_routes.py   (R2)

Factory: create_provenance_customs_router(service) -> APIRouter. Tenant via request.state.tenant.tenant_id.
"""
from fastapi import APIRouter, Request, HTTPException, Query
from aml.server.scopes import require_scope

from pydantic import BaseModel

from .provenance_customs import CustomsError, CustomsRejected, CorpusRegisterRequest


class RegisterCorpusRequest(BaseModel):
    name: str
    sources: list
    attestations: dict = {}
    processing: list = []
    metadata: dict = {}


def _tenant(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return ctx.tenant_id


def create_provenance_customs_router(service) -> APIRouter:
    router = APIRouter(prefix="/v1/provenance", tags=["Provenance Customs"])

    @router.post("/corpora", status_code=201)
    async def register_corpus(req: RegisterCorpusRequest, request: Request):
        require_scope(request, "artifacts:admin")
        try:
            return service.register_corpus(_tenant(request), CorpusRegisterRequest(**req.model_dump()))
        except CustomsRejected as e:
            raise HTTPException(status_code=422, detail={"status": "rejected", "reasons": e.reasons})
        except CustomsError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/stats")
    async def stats(request: Request):
        require_scope(request, "artifacts:read")
        return service.get_stats(_tenant(request))

    @router.get("/corpora")
    async def list_corpora(request: Request, limit: int = Query(50, le=200), offset: int = 0):
        require_scope(request, "artifacts:read")
        return service.list_corpora(_tenant(request), limit=limit, offset=offset)

    @router.get("/corpora/{corpus_id}")
    async def get_corpus(corpus_id: str, request: Request):
        require_scope(request, "artifacts:read")
        try:
            return service.get_corpus(_tenant(request), corpus_id)
        except CustomsError:
            raise HTTPException(status_code=404, detail="corpus not found")

    @router.get("/corpora/{corpus_id}/verify")
    async def verify(corpus_id: str, request: Request):
        require_scope(request, "artifacts:read")
        return service.verify_corpus(_tenant(request), corpus_id)

    @router.get("/corpora/{corpus_id}/proof")
    async def proof(corpus_id: str, request: Request, source_id: str = Query(...)):
        require_scope(request, "artifacts:read")
        return service.inclusion_proof(_tenant(request), corpus_id, source_id)

    @router.get("/corpora/{corpus_id}/block")
    async def block(corpus_id: str, request: Request):
        """The data_provenance block for R3 landing."""
        require_scope(request, "artifacts:read")
        return service.provenance_block(_tenant(request), corpus_id)

    return router
