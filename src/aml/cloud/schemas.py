"""
GRAFOMEM Shared Response Schemas — Pydantic models used across route files.

These models define the API *response* contract.  FastAPI uses them for:
  1. ``response_model=`` on route decorators → OpenAPI response schemas
  2. Automatic serialization filtering (only declared fields are sent)
  3. SDK contract validation (tests/test_openapi_contract.py)

Convention:
  - Request models live in each route file (co-located with the handler).
  - Response models that are reused across routes live here.
  - Route-specific response models may live in the route file itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ============================================================================
# Common envelope patterns
# ============================================================================

class DeletedResponse(BaseModel):
    """Standard response for DELETE operations."""
    deleted: bool
    detail: str = ""


class CountedListResponse(BaseModel):
    """Base for list endpoints that include a count."""
    count: int


# ============================================================================
# Memory / Store responses
# ============================================================================

class StoreResponse(BaseModel):
    store_id: str
    capabilities: list[str] = []


class WriteResult(BaseModel):
    ref: int
    stored: bool = True


class MemoryRecord(BaseModel):
    ref: int
    content: str
    metadata: dict[str, Any] = {}
    similarity: Optional[float] = None
    created_at: Optional[str] = None
    superseded_by: Optional[int] = None


class RetrieveResponse(BaseModel):
    memories: list[MemoryRecord] = []
    query: str = ""
    store_id: str = ""


class DeleteResult(BaseModel):
    deleted: bool
    ref: Optional[int] = None


class SupersedeResult(BaseModel):
    new_ref: int
    old_ref: int
    superseded: bool = True


class BatchWriteResult(BaseModel):
    refs: list[int] = []
    count: int = 0


class CapabilitiesResponse(BaseModel):
    capabilities: list[str] = []
    backend: str = ""
    store_id: str = ""


class IngestionStatsResponse(BaseModel):
    store_id: str
    total_memories: int = 0
    pending_batches: int = 0


class AuditResponse(BaseModel):
    store_id: str
    events: list[dict[str, Any]] = []
    count: int = 0


class FlushResponse(BaseModel):
    flushed: bool = True
    store_id: str = ""


# ============================================================================
# Governance responses
# ============================================================================

class PolicyResponse(BaseModel):
    policy_id: str
    name: str
    description: str = ""
    policy_type: str
    action: str
    config: dict[str, Any] = {}
    priority: int = 0
    enabled: bool = True
    created_at: Optional[str] = None


class EvaluationResultResponse(BaseModel):
    allowed: bool
    evaluations: list[dict[str, Any]] = []
    escalated: bool = False


class GovernanceStatsResponse(BaseModel):
    policies_total: int = 0
    policies_active: int = 0
    evaluations_total: int = 0
    denials_total: int = 0
    escalations_total: int = 0


class PolicyListResponse(BaseModel):
    policies: list[PolicyResponse] = []
    count: int = 0


class GovernanceLogResponse(BaseModel):
    logs: list[dict[str, Any]] = []
    count: int = 0


# ============================================================================
# Decision Trail responses
# ============================================================================

class DecisionRecordResponse(BaseModel):
    decision_id: str
    agent_id: Optional[str] = None
    model_id: Optional[str] = None
    input_hash: Optional[str] = None
    output_hash: Optional[str] = None
    output_text: Optional[str] = None
    retrieved_facts: Optional[list[dict[str, Any]]] = None
    token_count: int = 0
    signature: Optional[str] = None
    public_key: Optional[str] = None
    created_at: Optional[str] = None


class DecisionListResponse(BaseModel):
    decisions: list[DecisionRecordResponse] = []
    count: int = 0
    total: int = 0


class DecisionStatsResponse(BaseModel):
    total_decisions: int = 0
    total_tokens: int = 0
    models_used: list[str] = []


class ReplayResultResponse(BaseModel):
    decision_id: str
    status: str  # identical, diverged, degraded, error
    confidence: float = 0.0
    input_reconstructed: bool = False
    model_available: bool = False
    original_output: Optional[str] = None
    replay_output: Optional[str] = None


# ============================================================================
# Erasure responses
# ============================================================================

class ErasureCertificateResponse(BaseModel):
    certificate_id: str
    fact_ref: int
    fact_content_hash: Optional[str] = None
    memory_deleted: bool = False
    decisions_scrubbed: int = 0
    legal_basis: str = ""
    requested_by: str = ""
    signature: Optional[str] = None
    public_key: Optional[str] = None
    created_at: Optional[str] = None


class ErasureVerifyResponse(BaseModel):
    valid: bool
    detail: str = ""
    certificate_id: str = ""


class ErasureListResponse(BaseModel):
    certificates: list[ErasureCertificateResponse] = []
    count: int = 0


class ErasureStatsResponse(BaseModel):
    total_certificates: int = 0
    total_facts_erased: int = 0


# ============================================================================
# Orchestrator responses
# ============================================================================

class AgentResponse(BaseModel):
    agent_id: str
    name: str
    role: str
    description: str = ""
    model_id: str = ""
    fallback_models: list[str] = Field(default_factory=list)
    system_prompt: Optional[str] = None
    memory_stores: list[str] = []
    tools: list[str] = []
    max_steps: int = 5
    temperature: float = 0.5
    created_at: Optional[str] = None


class AgentListResponse(BaseModel):
    agents: list[AgentResponse] = []
    count: int = 0


class StepResponse(BaseModel):
    step_id: str
    agent_id: str
    agent_name: Optional[str] = None
    role: Optional[str] = None
    step_number: int = 0
    status: str = ""
    decision_id: Optional[str] = None
    parent_decision_id: Optional[str] = None
    input_text: Optional[str] = None
    output_text: Optional[str] = None
    token_count: int = 0
    latency_ms: int = 0
    latency_governance_ms: int = 0
    latency_memory_ms: int = 0
    latency_llm_ms: int = 0
    latency_tools_ms: int = 0
    governance_allowed: bool = True
    governance_logs: Optional[list[dict[str, Any]]] = None
    retrieved_facts: Optional[list[dict[str, Any]]] = None
    tools_called: Optional[list[dict[str, Any]]] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class WorkflowResponse(BaseModel):
    workflow_id: str
    name: str
    description: str = ""
    mode: str = "sequential"
    status: str = "created"
    agent_ids: list[str] = []
    steps: list[StepResponse] = []
    total_tokens: int = 0
    max_total_steps: int = 10
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowResponse] = []
    count: int = 0


class ReceiptResponse(BaseModel):
    receipt_id: str
    workflow_id: str
    step_number: int
    agent_id: Optional[str] = None
    input_hash: Optional[str] = None
    output_hash: Optional[str] = None
    memory_snapshot_hash: Optional[str] = None
    previous_receipt_hash: Optional[str] = None
    signature: Optional[str] = None
    public_key: Optional[str] = None
    created_at: Optional[str] = None


class ReceiptListResponse(BaseModel):
    receipts: list[ReceiptResponse] = []
    count: int = 0


class ChainVerificationResponse(BaseModel):
    status: str  # intact, tampered, empty
    steps_verified: int = 0
    tampered_at_step: Optional[int] = None


class OrchestratorStatsResponse(BaseModel):
    agents_total: int = 0
    workflows_total: int = 0
    steps_total: int = 0
    tokens_total: int = 0


# ============================================================================
# Regulatory Reports responses
# ============================================================================

class ReportSectionResponse(BaseModel):
    title: str
    article: str = ""
    status: str = ""  # COMPLIANT, PARTIAL, INSUFFICIENT_DATA
    evidence: dict[str, Any] = {}
    narrative: str = ""


class ReportResponse(BaseModel):
    report_id: str
    report_type: str
    framework: Optional[str] = None
    tenant_id: Optional[str] = None
    sections: list[ReportSectionResponse] = []
    overall_status: str = ""
    content_hash: Optional[str] = None
    created_at: Optional[str] = None


class ReportSummaryResponse(BaseModel):
    report_id: str
    report_type: str
    overall_status: str = ""
    created_at: Optional[str] = None


class ReportListResponse(BaseModel):
    reports: list[ReportSummaryResponse] = []
    count: int = 0


class ReportStatsResponse(BaseModel):
    total_reports: int = 0
    frameworks_used: list[str] = []


# ============================================================================
# LLM & Tools responses
# ============================================================================

class LLMProviderResponse(BaseModel):
    config_id: str
    provider: str
    model_id: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    created_at: Optional[str] = None


class LLMProviderListResponse(BaseModel):
    providers: list[LLMProviderResponse] = []
    count: int = 0


class ToolDefinitionResponse(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = {}
    builtin: bool = False


class ToolListResponse(BaseModel):
    tools: list[ToolDefinitionResponse] = []
    count: int = 0


# ============================================================================
# Webhook responses
# ============================================================================

class WebhookConfigResponse(BaseModel):
    webhook_id: str
    url: str
    events: list[str] = []
    enabled: bool = True
    description: str = ""
    created_at: Optional[str] = None


class WebhookCreatedResponse(WebhookConfigResponse):
    """Returned only on creation — includes the signing secret."""
    secret: str


class WebhookListResponse(BaseModel):
    webhooks: list[WebhookConfigResponse] = []
    count: int = 0


class WebhookDeliveryResponse(BaseModel):
    delivery_id: str
    webhook_id: str
    event_type: str
    status_code: Optional[int] = None
    success: bool = False
    attempt: int = 1
    created_at: Optional[str] = None


class WebhookDeliveryListResponse(BaseModel):
    deliveries: list[WebhookDeliveryResponse] = []
    count: int = 0


class WebhookTestResponse(BaseModel):
    test: bool = True
    delivery: WebhookDeliveryResponse


class WebhookEventTypesResponse(BaseModel):
    event_types: list[str] = []


# ============================================================================
# SSO responses
# ============================================================================

class SSOProviderInfo(BaseModel):
    provider: str
    name: str = ""
    enabled: bool = True


class SSOProviderListResponse(BaseModel):
    providers: list[SSOProviderInfo] = []


class SSOConfiguredResponse(BaseModel):
    config_id: str
    provider: str
    enabled: bool = True
    created_at: Optional[str] = None


# ============================================================================
# Monitoring responses (Sprint 13)
# ============================================================================

class HealthResponse(BaseModel):
    """Liveness probe response."""
    status: str  # ok
    uptime_seconds: float = 0.0
    started_at: Optional[str] = None
    version: str = "unknown"


class DependencyCheck(BaseModel):
    """Individual dependency check result."""
    status: str  # ok, error
    detail: str = ""
    via: str = ""


class ReadinessResponse(BaseModel):
    """Readiness probe response with dependency checks."""
    status: str  # ok, degraded
    checks: dict[str, DependencyCheck] = {}
    uptime_seconds: float = 0.0


class PoolStatsResponse(BaseModel):
    """Database connection pool statistics."""
    pooled: bool = False
    pool_min: int = 0
    pool_max: int = 0
    pool_size: int = 0
    pool_available: int = 0
    requests_waiting: int = 0


class MetricsSummaryResponse(BaseModel):
    """Prometheus metrics summary for portal dashboard."""
    available: bool = False
    governance: Optional[dict[str, float]] = None
    workflows: Optional[dict[str, float]] = None
    memory_operations: float = 0.0
    decisions_logged: float = 0.0
    erasure_certificates: float = 0.0
    webhooks_dispatched: float = 0.0
    sso_logins: float = 0.0


class MonitoringStatsResponse(BaseModel):
    """Full system monitoring stats."""
    status: str = "ok"
    uptime_seconds: float = 0.0
    started_at: Optional[str] = None
    version: str = "unknown"
    pool: PoolStatsResponse = PoolStatsResponse()
    stores: dict[str, Any] = {}
    metrics: MetricsSummaryResponse = MetricsSummaryResponse()
