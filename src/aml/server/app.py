"""
GRAFOMEM FastAPI server — production-grade async API for the GMP protocol.

Exposes the full MemoryBackend interface as REST endpoints with Pydantic models,
OpenAPI auto-documentation, and optional batched ingestion. The same conformance
suite that passes locally also passes over this HTTP layer (GMPClient IS a
MemoryBackend, and these endpoints match the wire.py contract).

Cloud mode (db_url != None): also mounts the /v1/cloud management endpoints
for tenant provisioning, compliance tracking, and usage metering.

Start via:  grafomem serve --host 0.0.0.0 --port 8642
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from aml.backends.interface import (
    Capability,
    CapabilityNotSupported,
    Memory,
    RetrieveOptions,
    SourceMeta,
    WriteOptions,
)
from aml.server.auth import DEFAULT_NAMESPACE, TenantAuthMiddleware
from aml.server.stores import StoreManager

logger = logging.getLogger("grafomem.server")


# ============================================================================
# Pydantic models — request / response
# ============================================================================

class WriteOptionsModel(BaseModel):
    valid_from: datetime | None = None
    tenant_id: str | None = None
    signing_key: str | None = None  # hex-encoded Ed25519 private seed
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_internal(self, tenant_override: str | None = None) -> WriteOptions:
        return WriteOptions(
            valid_from=self.valid_from,
            tenant_id=tenant_override or self.tenant_id,
            signing_key=bytes.fromhex(self.signing_key) if self.signing_key else None,
            metadata=self.metadata,
        )


class RetrieveOptionsModel(BaseModel):
    budget_tokens: int = 512
    as_of: datetime | None = None
    tenant_id: str | None = None
    top_k: int | None = None

    def to_internal(self, tenant_override: str | None = None) -> RetrieveOptions:
        return RetrieveOptions(
            budget_tokens=self.budget_tokens,
            as_of=self.as_of,
            tenant_id=tenant_override or self.tenant_id,
            top_k=self.top_k,
        )


class WriteRequest(BaseModel):
    content: str
    options: WriteOptionsModel = WriteOptionsModel()


class WriteBatchRequest(BaseModel):
    items: list[WriteRequest]


class SupersedeRequest(BaseModel):
    old_ref: Any
    content: str
    options: WriteOptionsModel = WriteOptionsModel()


class DeleteRequest(BaseModel):
    ref: Any


class RetrieveRequest(BaseModel):
    query: str
    options: RetrieveOptionsModel = RetrieveOptionsModel()


class SourceMetaResponse(BaseModel):
    write_id: str | None = None
    written_at: datetime | None = None
    written_by: str | None = None
    signature: str | None = None   # hex-encoded
    public_key: str | None = None  # hex-encoded

    @classmethod
    def from_internal(cls, s: SourceMeta | None):
        if s is None:
            return None
        return cls(
            write_id=s.write_id,
            written_at=s.written_at,
            written_by=s.written_by,
            signature=s.signature.hex() if s.signature else None,
            public_key=s.public_key.hex() if s.public_key else None,
        )


class MemoryResponse(BaseModel):
    ref: Any
    content: str
    written_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    tenant_id: str | None = None
    superseded_by: Any | None = None
    source: SourceMetaResponse | None = None

    @classmethod
    def from_internal(cls, m: Memory):
        return cls(
            ref=m.ref,
            content=m.content,
            written_at=m.written_at,
            metadata=m.metadata or {},
            valid_from=m.valid_from,
            valid_until=m.valid_until,
            tenant_id=m.tenant_id,
            superseded_by=m.superseded_by,
            source=SourceMetaResponse.from_internal(m.source),
        )


# ============================================================================
# Helpers
# ============================================================================

def _get_store(request: Request, store_id: str):
    mgr: StoreManager = request.app.state.store_manager
    entry = mgr.get(store_id)
    if entry is None:
        raise HTTPException(404, f"Store '{store_id}' not found")
    return entry


def _tenant_id(request: Request) -> str | None:
    """Extract tenant_id from auth middleware. Returns None for default namespace."""
    ctx = getattr(request.state, "tenant", None)
    if ctx is None or ctx.tenant_id == DEFAULT_NAMESPACE:
        return None
    return ctx.tenant_id


# ============================================================================
# Router — all GMP endpoints
# ============================================================================

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    mgr: StoreManager = request.app.state.store_manager
    return {
        "status": "ok",
        "version": "0.2.0",
        "stores": mgr.count,
    }


@router.post("/v1/stores")
async def create_store(request: Request):
    mgr: StoreManager = request.app.state.store_manager
    store_id = mgr.create()
    return {"store_id": store_id}


@router.get("/v1/stores")
async def list_stores(request: Request):
    mgr: StoreManager = request.app.state.store_manager
    return {"stores": mgr.list_stores()}


@router.get("/v1/stores/{store_id}/capabilities")
async def get_capabilities(store_id: str, request: Request):
    entry = _get_store(request, store_id)
    caps = entry.backend.capabilities()
    return {"capabilities": sorted(c.value for c in caps)}


@router.post("/v1/stores/{store_id}/write")
async def write_memory(store_id: str, req: WriteRequest, request: Request):
    entry = _get_store(request, store_id)
    tenant = _tenant_id(request)
    opts = req.options.to_internal(tenant_override=tenant)

    try:
        ref = entry.backend.write(req.content, opts)
    except CapabilityNotSupported as e:
        raise HTTPException(422, {
            "error": "capability_not_supported",
            "capability": e.args[0].value,
            "operation": e.args[1],
        })
    return {"ref": ref}


@router.post("/v1/stores/{store_id}/write_batch")
async def write_batch(store_id: str, req: WriteBatchRequest, request: Request):
    entry = _get_store(request, store_id)
    tenant = _tenant_id(request)

    if not hasattr(entry.backend, "write_many"):
        raise HTTPException(501, "Backend does not support batch writes")

    items = [(r.content, r.options.to_internal(tenant_override=tenant)) for r in req.items]
    try:
        refs = entry.backend.write_many(items)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"refs": refs}


@router.post("/v1/stores/{store_id}/supersede")
async def supersede_memory(store_id: str, req: SupersedeRequest, request: Request):
    entry = _get_store(request, store_id)
    tenant = _tenant_id(request)
    opts = req.options.to_internal(tenant_override=tenant)

    try:
        ref = entry.backend.supersede(req.old_ref, req.content, opts)
    except CapabilityNotSupported as e:
        raise HTTPException(422, {
            "error": "capability_not_supported",
            "capability": e.args[0].value,
            "operation": e.args[1],
        })
    return {"ref": ref}


@router.post("/v1/stores/{store_id}/delete")
async def delete_memory(store_id: str, req: DeleteRequest, request: Request):
    entry = _get_store(request, store_id)
    try:
        deleted = entry.backend.delete(req.ref)
    except CapabilityNotSupported as e:
        raise HTTPException(422, {
            "error": "capability_not_supported",
            "capability": e.args[0].value,
            "operation": e.args[1],
        })
    return {"deleted": deleted}


@router.post("/v1/stores/{store_id}/retrieve")
async def retrieve_memories(store_id: str, req: RetrieveRequest, request: Request):
    entry = _get_store(request, store_id)
    tenant = _tenant_id(request)
    opts = req.options.to_internal(tenant_override=tenant)

    try:
        mems = entry.backend.retrieve(req.query, opts)
    except CapabilityNotSupported as e:
        raise HTTPException(422, {
            "error": "capability_not_supported",
            "capability": e.args[0].value,
            "operation": e.args[1],
        })
    return {"memories": [MemoryResponse.from_internal(m).model_dump() for m in mems]}


@router.get("/v1/stores/{store_id}/audit")
async def audit_memories(store_id: str, request: Request):
    entry = _get_store(request, store_id)
    mems = list(entry.backend.audit())
    return {"memories": [MemoryResponse.from_internal(m).model_dump() for m in mems]}


@router.post("/v1/stores/{store_id}/flush")
async def flush_store(store_id: str, request: Request):
    entry = _get_store(request, store_id)
    entry.backend.flush()
    return {}


@router.get("/v1/stores/{store_id}/ingestion/stats")
async def ingestion_stats(store_id: str, request: Request):
    queues = getattr(request.app.state, "ingestion_queues", {})
    queue = queues.get(store_id)
    if queue is None:
        return {"batching_enabled": False}
    return {"batching_enabled": True, **queue.stats()}


# ============================================================================
# App factory
# ============================================================================

def create_app(
    backend_factory=None,
    *,
    auth_mode: str = "none",
    tokens: dict[str, str] | None = None,
    enable_batching: bool = False,
    batch_size: int = 64,
    flush_interval_ms: int = 50,
    db_url: str | None = None,
) -> FastAPI:
    """Create the FastAPI application.

    Parameters
    ----------
    backend_factory : callable
        A no-arg callable that returns a fresh MemoryBackend instance.
    auth_mode : str
        "none" or "token".
    tokens : dict
        Token → tenant_id mapping (only used when auth_mode="token").
    enable_batching : bool
        If True, writes go through the IngestionQueue for batched embedding.
    db_url : str | None
        PostgreSQL connection URL. When provided, enables the cloud management
        layer (tenant provisioning, compliance tracking, usage metering) and
        mounts the /v1/cloud endpoints.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("GRAFOMEM server starting")
        yield
        # Shutdown: stop all ingestion queues
        for q in getattr(app.state, "ingestion_queues", {}).values():
            await q.stop()
        # Shutdown: close cloud services
        for svc_name in ("tenant_manager", "compliance_tracker", "metering_service"):
            svc = getattr(app.state, svc_name, None)
            if svc is not None and hasattr(svc, "close"):
                svc.close()
        logger.info("GRAFOMEM server stopped")

    app = FastAPI(
        title="GRAFOMEM",
        description="GMP-conformant agent memory server",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Auth is the inner layer; CORS is added LAST so it's the OUTERMOST
    # middleware and can answer preflight OPTIONS before auth inspects them.
    app.add_middleware(TenantAuthMiddleware, auth_mode=auth_mode, tokens=tokens)

    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://grafomem.com",
            "https://www.grafomem.com",
            "https://docs.grafomem.com",
        ],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # Attach store manager to app state (no module-level globals)
    factory = backend_factory or _default_factory
    app.state.store_manager = StoreManager(factory)
    app.state.ingestion_queues = {}
    app.state.enable_batching = enable_batching
    app.state.batch_size = batch_size
    app.state.flush_interval_ms = flush_interval_ms

    # Include the router with all GMP endpoints
    app.include_router(router)

    # Cloud management layer — only when db_url is provided
    if db_url is not None:
        try:
            from aml.cloud.tenant_manager import TenantManager
            from aml.cloud.compliance import ComplianceTracker
            from aml.cloud.metering import MeteringService
            from aml.cloud.routes import router as cloud_router

            tm = TenantManager(db_url)
            tm.ensure_schema()
            app.state.tenant_manager = tm

            ct = ComplianceTracker(db_url)
            ct.ensure_schema()
            app.state.compliance_tracker = ct

            ms = MeteringService(db_url)
            ms.ensure_schema()
            app.state.metering_service = ms

            app.include_router(cloud_router)
            logger.info("Cloud management layer enabled (/v1/cloud)")
        except ImportError as e:
            logger.warning("Cloud layer unavailable (missing deps): %s", e)
        except Exception as e:
            logger.warning("Cloud layer failed to initialize: %s", e)

    return app


def _default_factory():
    from aml.backends.sqlite_gmp import SQLiteGMPBackend
    return SQLiteGMPBackend(":memory:")
