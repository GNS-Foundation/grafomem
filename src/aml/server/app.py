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
import os
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

def _get_store(request: Request, store_id: str, tenant_id: str | None = None):
    mgr: StoreManager = request.app.state.store_manager
    entry = mgr.get(store_id)
    if entry is None:
        raise HTTPException(404, f"Store '{store_id}' not found")
    # Enforce tenant isolation: if store has an owner and caller has a tenant,
    # they must match. Stores without an owner (legacy/non-cloud) remain open.
    if (
        tenant_id is not None
        and entry.owner_tenant_id is not None
        and entry.owner_tenant_id != tenant_id
    ):
        raise HTTPException(403, "Access denied: store belongs to another tenant")
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
    tenant = _tenant_id(request)
    store_id = mgr.create(tenant_id=tenant)
    return {"store_id": store_id}


@router.get("/v1/stores")
async def list_stores(request: Request):
    mgr: StoreManager = request.app.state.store_manager
    return {"stores": mgr.list_stores()}


@router.get("/v1/stores/{store_id}/capabilities")
async def get_capabilities(store_id: str, request: Request):
    entry = _get_store(request, store_id, _tenant_id(request))
    caps = entry.backend.capabilities()
    return {"capabilities": sorted(c.value for c in caps)}


@router.post("/v1/stores/{store_id}/write")
async def write_memory(store_id: str, req: WriteRequest, request: Request):
    tenant = _tenant_id(request)
    entry = _get_store(request, store_id, tenant)
    opts = req.options.to_internal(tenant_override=tenant)

    try:
        ref = entry.backend.write(req.content, opts)
    except CapabilityNotSupported as e:
        raise HTTPException(422, {
            "error": "capability_not_supported",
            "capability": e.args[0].value,
            "operation": e.args[1],
        })
    try:
        from aml.cloud.metrics import MEMORY_OPERATIONS
        MEMORY_OPERATIONS.labels(operation="write").inc()
    except Exception:
        pass
    return {"ref": ref}


@router.post("/v1/stores/{store_id}/write_batch")
async def write_batch(store_id: str, req: WriteBatchRequest, request: Request):
    tenant = _tenant_id(request)
    entry = _get_store(request, store_id, tenant)

    if not hasattr(entry.backend, "write_many"):
        raise HTTPException(501, "Backend does not support batch writes")

    items = [(r.content, r.options.to_internal(tenant_override=tenant)) for r in req.items]
    try:
        refs = entry.backend.write_many(items)
    except Exception as e:
        raise HTTPException(500, str(e))
    try:
        from aml.cloud.metrics import MEMORY_OPERATIONS
        MEMORY_OPERATIONS.labels(operation="batch_write").inc(len(refs))
    except Exception:
        pass
    return {"refs": refs}


@router.post("/v1/stores/{store_id}/supersede")
async def supersede_memory(store_id: str, req: SupersedeRequest, request: Request):
    tenant = _tenant_id(request)
    entry = _get_store(request, store_id, tenant)
    opts = req.options.to_internal(tenant_override=tenant)

    try:
        ref = entry.backend.supersede(req.old_ref, req.content, opts)
    except CapabilityNotSupported as e:
        raise HTTPException(422, {
            "error": "capability_not_supported",
            "capability": e.args[0].value,
            "operation": e.args[1],
        })
    try:
        from aml.cloud.metrics import MEMORY_OPERATIONS
        MEMORY_OPERATIONS.labels(operation="supersede").inc()
    except Exception:
        pass
    return {"ref": ref}


@router.post("/v1/stores/{store_id}/delete")
async def delete_memory(store_id: str, req: DeleteRequest, request: Request):
    entry = _get_store(request, store_id, _tenant_id(request))
    try:
        deleted = entry.backend.delete(req.ref)
    except CapabilityNotSupported as e:
        raise HTTPException(422, {
            "error": "capability_not_supported",
            "capability": e.args[0].value,
            "operation": e.args[1],
        })
    try:
        from aml.cloud.metrics import MEMORY_OPERATIONS
        MEMORY_OPERATIONS.labels(operation="delete").inc()
    except Exception:
        pass
    return {"deleted": deleted}


@router.post("/v1/stores/{store_id}/retrieve")
async def retrieve_memories(store_id: str, req: RetrieveRequest, request: Request):
    tenant = _tenant_id(request)
    entry = _get_store(request, store_id, tenant)
    opts = req.options.to_internal(tenant_override=tenant)

    try:
        mems = entry.backend.retrieve(req.query, opts)
    except CapabilityNotSupported as e:
        raise HTTPException(422, {
            "error": "capability_not_supported",
            "capability": e.args[0].value,
            "operation": e.args[1],
        })
    try:
        from aml.cloud.metrics import MEMORY_OPERATIONS
        MEMORY_OPERATIONS.labels(operation="retrieve").inc()
    except Exception:
        pass
    return {"memories": [MemoryResponse.from_internal(m).model_dump() for m in mems]}


@router.get("/v1/stores/{store_id}/audit")
async def audit_memories(store_id: str, request: Request):
    entry = _get_store(request, store_id, _tenant_id(request))
    mems = list(entry.backend.audit())
    return {"memories": [MemoryResponse.from_internal(m).model_dump() for m in mems]}


@router.post("/v1/stores/{store_id}/flush")
async def flush_store(store_id: str, request: Request):
    entry = _get_store(request, store_id, _tenant_id(request))
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
    stripe_secret_key: str | None = None,
    stripe_webhook_secret: str | None = None,
    portal_secret_key: str | None = None,
    spec_only: bool = False,
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
    stripe_secret_key : str | None
        Stripe API secret key. Enables billing integration.
    stripe_webhook_secret : str | None
        Stripe webhook signing secret.
    portal_secret_key : str | None
        Secret for signing portal JWT tokens.
    spec_only : bool
        If True, register all routes but skip ``ensure_schema()`` calls and
        connection pool creation.  Useful for generating the complete OpenAPI
        spec without requiring a live database.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("GRAFOMEM server starting")
        # Initialize connection pool if db_url is provided
        if db_url and not spec_only:
            try:
                from aml.cloud.db_pool import RoutingPool
                pool = RoutingPool(db_url)
                pool.open()
                app.state.db_pool = pool
                logger.info(
                    "Database pool initialized (replica=%s)",
                    pool.has_replica,
                )
            except Exception as e:
                logger.warning("Connection pool unavailable: %s", e)
                app.state.db_pool = None
        else:
            app.state.db_pool = None
        yield
        # Shutdown: stop all ingestion queues
        for q in getattr(app.state, "ingestion_queues", {}).values():
            await q.stop()
        # Shutdown: close cloud services
        for svc_name in ("tenant_manager", "compliance_tracker", "metering_service",
                         "decision_trail", "erasure_proof", "governance_gateway",
                         "regulatory_reports", "llm_registry", "tool_registry",
                         "orchestrator", "portal_auth", "stripe_billing",
                         "webhook_service", "assurance_service"):
            svc = getattr(app.state, svc_name, None)
            if svc is not None and hasattr(svc, "close"):
                svc.close()
        # Close database pool last
        pool = getattr(app.state, "db_pool", None)
        if pool is not None:
            pool.close()
        logger.info("GRAFOMEM server stopped")

    app = FastAPI(
        title="GRAFOMEM Cloud",
        description=(
            "GMP-conformant governed agent memory platform.  Provides "
            "vector-backed memory stores, multi-agent orchestration, "
            "decision trail audit logging, erasure proof certificates, "
            "governance policy evaluation, regulatory reports, webhook "
            "alerts, SSO/OIDC, and real-time SSE streaming."
        ),
        version="1.9.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=[
            {"name": "Memory Stores", "description": "CRUD for vector-backed memory stores"},
            {"name": "Orchestrator", "description": "Multi-agent workflow orchestration"},
            {"name": "Decision Trail", "description": "Inference audit logging and replay"},
            {"name": "Governance", "description": "Policy evaluation and enforcement"},
            {"name": "Erasure Proof", "description": "GDPR erasure certificates"},
            {"name": "Regulatory Reports", "description": "Compliance report generation"},
            {"name": "LLM & Tools", "description": "LLM provider and tool management"},
            {"name": "Webhooks", "description": "Event webhook management"},
            {"name": "Portal", "description": "Tenant portal authentication"},
            {"name": "SSO", "description": "OAuth2/OIDC single sign-on"},
            {"name": "Cloud Admin", "description": "Tenant and billing management"},
        ],
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Sprint 13: Prometheus metrics middleware (innermost layer)
    # ------------------------------------------------------------------
    try:
        from aml.cloud.metrics import PrometheusMiddleware, metrics_endpoint
        app.add_middleware(PrometheusMiddleware)
        app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
        logger.info("Prometheus metrics enabled (/metrics)")
    except Exception as e:
        logger.info("Prometheus metrics not available: %s", e)

    # ------------------------------------------------------------------
    # Sprint 13: Health endpoints (no auth required)
    # ------------------------------------------------------------------
    from aml.cloud.health import HealthChecker

    health_checker = HealthChecker(app, db_url=db_url)
    app.state.health_checker = health_checker

    @app.get("/healthz", include_in_schema=False)
    async def health_liveness():
        """Kubernetes liveness probe — always 200 if server is running."""
        return health_checker.liveness()

    @app.get("/readyz", include_in_schema=False)
    async def health_readiness():
        """Kubernetes readiness probe — checks downstream dependencies."""
        result = health_checker.readiness()
        status_code = 200 if result["status"] == "ok" else 503
        from fastapi.responses import JSONResponse
        return JSONResponse(content=result, status_code=status_code)

    @app.get("/v1/monitoring/stats", tags=["Cloud Admin"])
    async def monitoring_stats(request: Request):
        """Full system stats for monitoring dashboard. Requires auth."""
        # Auth is enforced by the TenantAuthMiddleware
        return health_checker.full_stats()

    # Auth is the inner layer; CORS is added LAST so it's the OUTERMOST
    # middleware and can answer preflight OPTIONS before auth inspects them.
    # When db_url is provided, use "cloud" auth to resolve X-API-Key from tenants table.
    effective_auth_mode = auth_mode
    if db_url and auth_mode == "none":
        effective_auth_mode = "cloud"
    app.add_middleware(
        TenantAuthMiddleware,
        auth_mode=effective_auth_mode,
        tokens=tokens,
        db_url=db_url,
    )

    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://grafomem.com",
            "https://www.grafomem.com",
            "https://cloud.grafomem.com",
            "https://docs.grafomem.com",
            "http://localhost:3000",
            "http://localhost:3001",
            "http://localhost:3002",
            "http://localhost:3003",
            "http://localhost:8642",
        ],
        allow_methods=["GET", "POST", "OPTIONS", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
        allow_credentials=True,
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
        pool = getattr(app.state, "db_pool", None)
        try:
            # In spec_only mode we skip ensure_schema() — routes still mount
            def _init(svc):
                if not spec_only:
                    svc.ensure_schema()
                return svc

            from aml.cloud.tenant_manager import TenantManager
            from aml.cloud.compliance import ComplianceTracker
            from aml.cloud.metering import MeteringService
            from aml.cloud.routes import router as cloud_router

            tm = TenantManager(db_url, pool=pool)
            _init(tm)
            app.state.tenant_manager = tm

            ct = ComplianceTracker(db_url, pool=pool)
            _init(ct)
            app.state.compliance_tracker = ct

            ms = MeteringService(db_url, pool=pool)
            _init(ms)
            app.state.metering_service = ms

            app.include_router(cloud_router)
            logger.info("Cloud management layer enabled (/v1/cloud)")

            # Decision Trail — inference audit logging
            from aml.cloud.decision_trail import DecisionTrailService
            from aml.cloud.decision_routes import create_decision_router

            dt = DecisionTrailService(db_url, pool=pool)
            _init(dt)
            app.state.decision_trail = dt

            decision_router = create_decision_router(
                dt, app.state.store_manager,
            )
            app.include_router(decision_router)
            logger.info("Decision Trail enabled (/v1/decisions)")

            # Erasure Proof — GDPR Article 17 signed certificates
            from aml.cloud.erasure_proof import ErasureProofService
            from aml.cloud.erasure_routes import create_erasure_router

            # Use ERASURE_SIGNING_KEY env var if available
            erasure_key_hex = os.environ.get("ERASURE_SIGNING_KEY")
            erasure_key = bytes.fromhex(erasure_key_hex) if erasure_key_hex else None

            ep = ErasureProofService(db_url, decision_trail=dt, signing_key=erasure_key, pool=pool)
            _init(ep)
            app.state.erasure_proof = ep

            erasure_router = create_erasure_router(ep)
            app.include_router(erasure_router)
            logger.info("Erasure Proof enabled (/v1/erasure)")

            # Governance Gateway — policy-as-code
            from aml.cloud.governance import GovernanceGateway
            from aml.cloud.governance_routes import create_governance_router

            gg = GovernanceGateway(db_url, pool=pool)
            _init(gg)
            app.state.governance_gateway = gg

            # gcrumbs — breadcrumb chain + Merkle epoch anchor (Sprint 15)
            from aml.cloud.gcrumbs import GcrumbsService
            from aml.cloud.gcrumbs_routes import create_gcrumbs_router

            gc = GcrumbsService(db_url, signing_key=erasure_key, pool=pool)
            _init(gc)
            app.state.gcrumbs = gc
            app.include_router(create_gcrumbs_router(gc))
            logger.info("gcrumbs enabled (/v1/gcrumbs)")

            from aml.cloud.landing_service import LandingService
            from aml.cloud.landing_routes import create_landing_router
            ls = LandingService(db_url, signing_key=erasure_key, gateway=gg, decision_trail=dt,
                                epoch_anchor=False, gcrumbs=gc, pool=pool)
            _init(ls)
            app.state.landing_service = ls
            app.include_router(create_landing_router(ls))

            from aml.cloud.artifact_registry import ArtifactRegistryService
            from aml.cloud.artifact_registry_routes import create_artifact_registry_router
            ar = ArtifactRegistryService(db_url, signing_key=erasure_key, gateway=gg, decision_trail=dt, pool=pool)
            _init(ar)
            app.state.artifact_registry = ar
            app.include_router(create_artifact_registry_router(ar))
            ls.registry = ar

            from aml.cloud.world_model import WorldModelService
            from aml.cloud.world_model_routes import create_world_model_router
            wm = WorldModelService(db_url, signing_key=erasure_key, gateway=gg, decision_trail=dt, gcrumbs=gc, pool=pool)
            _init(wm)
            app.state.world_model = wm
            app.include_router(create_world_model_router(wm))

            # Sprint 28: Ontological Templates
            from aml.cloud.template_routes import get_template_routes
            app.include_router(get_template_routes(wm), prefix="/v1/templates")
            logger.info("Ontological Templates enabled (/v1/templates)")

            # R2 — Data-Provenance Customs
            from aml.cloud.provenance_customs import ProvenanceCustomsService
            from aml.cloud.provenance_customs_routes import create_provenance_customs_router
            pc = ProvenanceCustomsService(db_url, signing_key=erasure_key, gateway=gg, decision_trail=dt, gcrumbs=gc, pool=pool)
            _init(pc)
            app.state.provenance_customs = pc
            app.include_router(create_provenance_customs_router(pc))

            # R4 — Composition Governance
            from aml.cloud.composition_governance import CompositionGovernanceService
            from aml.cloud.composition_governance_routes import create_composition_governance_router
            cg = CompositionGovernanceService(db_url, signing_key=erasure_key, gateway=gg, decision_trail=dt, gcrumbs=gc, pool=pool)
            _init(cg)
            app.state.composition_governance = cg
            app.include_router(create_composition_governance_router(cg))

            # Wire gcrumbs into erasure (constructed before gateway, so set post-hoc)
            ep._gcrumbs = gc

            gov_router = create_governance_router(gg)
            app.include_router(gov_router)
            logger.info("Governance Gateway enabled (/v1/governance)")

            # Regulatory Reports — one-click compliance packages
            from aml.cloud.regulatory import RegulatoryReportService
            from aml.cloud.regulatory_routes import create_regulatory_router

            rr = RegulatoryReportService(
                db_url,
                decision_trail=dt,
                erasure_proof=ep,
                governance=gg,
                compliance=getattr(app.state, "compliance_tracker", None),
                pool=pool,
            )
            _init(rr)
            app.state.regulatory_reports = rr

            reg_router = create_regulatory_router(rr)
            app.include_router(reg_router)
            logger.info("Regulatory Reports enabled (/v1/reports)")

            # LLM Registry — Bring Your Own Model
            from aml.cloud.llm_registry import LLMRegistry
            from aml.cloud.tool_registry import ToolRegistry
            from aml.cloud.llm_routes import create_llm_router

            llm_reg = LLMRegistry(db_url, pool=pool)
            _init(llm_reg)
            app.state.llm_registry = llm_reg

            tool_reg = ToolRegistry(
                db_url,
                governance=gg,
                store_manager=app.state.store_manager,
                erasure_proof=ep,
                pool=pool,
            )
            _init(tool_reg)
            app.state.tool_registry = tool_reg

            llm_router = create_llm_router(llm_reg, tool_reg)
            app.include_router(llm_router)
            logger.info("LLM Registry + Tool Registry enabled (/v1/llm)")

            # Agent Orchestrator — governed multi-agent execution
            from aml.cloud.orchestrator import OrchestratorService
            from aml.cloud.orchestrator_routes import create_orchestrator_router

            orch = OrchestratorService(
                db_url,
                governance=gg,
                decision_trail=dt,
                erasure_proof=ep,
                store_manager=app.state.store_manager,
                llm_registry=llm_reg,
                tool_registry=tool_reg,
                gcrumbs=gc,
                pool=pool,
            )
            _init(orch)
            app.state.orchestrator = orch

            orch_router = create_orchestrator_router(orch)
            app.include_router(orch_router)
            logger.info("Agent Orchestrator enabled (/v1/orchestrator)")

            # Sprint 7a: Policy Engine + Evidence Collector
            # (Automatically wired via GovernanceGateway constructor)
            logger.info("Policy Engine + Evidence Collector active (via GovernanceGateway)")

            # Sprint 7b: Execution Receipts — hash-chained attestation
            from aml.cloud.execution_receipts import ExecutionReceiptService
            receipt_svc = ExecutionReceiptService(db_url, pool=pool)
            _init(receipt_svc)
            app.state.execution_receipts = receipt_svc
            orch._execution_receipts = receipt_svc
            logger.info("Execution Receipt Service enabled")

            # Sprint 7c: Memory Taxonomy — workflow context
            from aml.cloud.memory_taxonomy import WorkflowContextService
            wf_ctx = WorkflowContextService(db_url, pool=pool)
            _init(wf_ctx)
            app.state.workflow_context = wf_ctx
            orch._workflow_context = wf_ctx
            logger.info("Workflow Context Service enabled")

            # Sprint 7d: Deterministic Replay Engine
            from aml.cloud.replay_engine import ReplayEngine
            replay = ReplayEngine(
                db_url,
                decision_trail=dt,
                llm_registry=llm_reg,
                store_manager=app.state.store_manager,
                orchestrator=orch,
                pool=pool,
            )
            _init(replay)
            app.state.replay_engine = replay
            logger.info("Replay Engine enabled")

            # Sprint 11a: Webhook Alerts
            from aml.cloud.webhook_service import WebhookService
            from aml.cloud.webhook_routes import create_webhook_router

            wh = WebhookService(db_url, pool=pool)
            _init(wh)
            app.state.webhook_service = wh
            # Wire into governance for deny/escalate dispatch
            gg._webhook_service = wh
            # Wire into orchestrator for workflow complete/error dispatch
            orch._webhook_service = wh

            wh_router = create_webhook_router(wh)
            app.include_router(wh_router)
            logger.info("Webhook Alerts enabled (/v1/webhooks)")

            # Sprint 19: Continuous Assurance
            from aml.cloud.assurance import AssuranceService
            from aml.cloud.assurance_routes import router as assurance_router

            assurance_svc = AssuranceService(
                db_url, pool=pool,
                health_checker=health_checker,
            )
            _init(assurance_svc)
            app.state.assurance_service = assurance_svc
            app.include_router(assurance_router)
            logger.info("Continuous Assurance enabled (/v1/assurance)")

            # Sprint 22: Tenant Admin — member management + RBAC
            from aml.cloud.admin_routes import router as admin_router
            if not spec_only:
                tm.ensure_members_schema()
            app.include_router(admin_router, prefix="/v1/admin")
            logger.info("Tenant Admin enabled (/v1/admin)")

            # Sprint 23: Audit Export — compliance-ready bulk exports
            from aml.cloud.audit_export import AuditExportService
            from aml.cloud.audit_export_routes import router as audit_export_router

            audit_export = AuditExportService(
                decision_trail=dt,
                governance=gg,
                gcrumbs=gc,
                signing_key=erasure_key,
            )
            app.state.audit_export_service = audit_export
            app.include_router(audit_export_router, prefix="/v1/audit/export")
            logger.info("Audit Export enabled (/v1/audit/export)")
        except ImportError as e:
            logger.warning("Cloud layer unavailable (missing deps): %s", e)
        except Exception as e:
            logger.warning("Cloud layer failed to initialize: %s", e)

        # Portal auth — email/password signup + JWT sessions
        try:
            from aml.cloud.portal_auth import PortalAuth
            from aml.cloud.portal_routes import router as portal_router

            pa = PortalAuth(db_url, secret_key=portal_secret_key, pool=pool)
            _init(pa)
            app.state.portal_auth = pa
            app.include_router(portal_router)
            logger.info("Portal auth enabled (/v1/portal)")

            # Sprint 11c: SSO / OIDC
            try:
                from aml.cloud.sso_provider import SSOProvider
                from aml.cloud.sso_routes import create_sso_router

                redirect_base = os.environ.get(
                    "GRAFOMEM_SSO_REDIRECT_BASE",
                    "https://grafomem-production.up.railway.app",
                )
                sso = SSOProvider(db_url, portal_auth=pa, redirect_base=redirect_base, pool=pool)
                _init(sso)
                app.state.sso_provider = sso

                sso_router = create_sso_router(sso)
                app.include_router(sso_router)
                logger.info("SSO/OIDC enabled (/v1/portal/sso)")
            except Exception as e:
                logger.warning("SSO unavailable: %s", e)
        except ImportError as e:
            logger.warning("Portal auth unavailable (missing deps): %s", e)
        except Exception as e:
            logger.warning("Portal auth failed to initialize: %s", e)

        # Stripe billing
        try:
            from aml.cloud.stripe_billing import StripeBillingService

            sb_key = stripe_secret_key or os.environ.get("STRIPE_SECRET_KEY")
            if sb_key:
                sb = StripeBillingService(
                    db_url, sb_key,
                    webhook_secret=stripe_webhook_secret,
                )
                _init(sb)
                app.state.stripe_billing = sb
                logger.info("Stripe billing enabled")
        except ImportError as e:
            logger.warning("Stripe billing unavailable (missing deps): %s", e)
        except Exception as e:
            logger.warning("Stripe billing failed to initialize: %s", e)

    # Serve static portal files
    try:
        import importlib.resources
        portal_dir = str(importlib.resources.files("aml") / "static" / "portal")
        landing_dir = str(importlib.resources.files("aml") / "static" / "landing")
        from fastapi.staticfiles import StaticFiles
        import os as _os
        if _os.path.isdir(portal_dir):
            app.mount("/portal", StaticFiles(directory=portal_dir, html=True), name="portal")
            logger.info("Portal UI mounted at /portal")
        if _os.path.isdir(landing_dir):
            app.mount("/landing", StaticFiles(directory=landing_dir, html=False), name="landing-assets")
            # Serve index.html at root
            from fastapi.responses import FileResponse
            @app.get("/", include_in_schema=False)
            async def landing_page():
                return FileResponse(_os.path.join(landing_dir, "index.html"))
            logger.info("Landing page mounted at /")
    except Exception as e:
        logger.warning("Portal static files not available: %s", e)

    return app


def _default_factory():
    from aml.backends.sqlite_gmp import SQLiteGMPBackend
    return SQLiteGMPBackend(":memory:")
