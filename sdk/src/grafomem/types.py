"""Pydantic v2 models for the GRAFOMEM Cloud API.

Every API response is deserialized into one of these typed models so
callers get IDE autocompletion, type checking, and a clear contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Stores ────────────────────────────────────────────────────────────

class Store(BaseModel):
    """A memory store instance."""
    id: str = Field(default="", alias="store_id")
    name: str = ""
    backend: str = ""
    created_at: Optional[str] = None

    model_config = {"populate_by_name": True}


# ── Memories ──────────────────────────────────────────────────────────

class WriteResult(BaseModel):
    """Result of a memory write operation."""
    ref: Any = 0
    content_hash: str = ""
    status: str = "ok"


class MemoryRecord(BaseModel):
    """A retrieved memory record."""
    ref: Any = 0
    content: str = ""
    written_at: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    superseded_by: Any = None

    @property
    def text(self) -> str:
        """Alias for ``content`` (convenience)."""
        return self.content


class RetrieveResponse(BaseModel):
    """Response from a retrieve operation."""
    results: list[MemoryRecord] = Field(default_factory=list)
    query: str = ""
    store_id: str = ""


# ── Governance ────────────────────────────────────────────────────────

class Policy(BaseModel):
    """A governance policy."""
    id: str = Field(default="", alias="policy_id")
    name: str = ""
    description: str = ""
    policy_type: str = ""
    action: str = "deny"
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    priority: int = 0
    created_at: Optional[str] = None

    model_config = {"populate_by_name": True}


class EvaluationLog(BaseModel):
    """A single evaluation log entry."""
    policy_name: str = ""
    policy_type: str = ""
    action: str = ""
    result: str = ""
    detail: str = ""


class EvaluationResult(BaseModel):
    """Result of a governance evaluation."""
    allowed: bool = True
    logs: list[EvaluationLog] = Field(default_factory=list)
    escalated: bool = False


class GovernanceStats(BaseModel):
    """Governance summary statistics."""
    total_evaluations: int = 0
    total_policies: int = 0
    active_policies: int = 0
    denials: int = 0
    escalations: int = 0


# ── Decision Trail ────────────────────────────────────────────────────

class DecisionRecord(BaseModel):
    """A logged inference decision."""
    id: str = Field(default="", alias="decision_id")
    decision_id: str = ""
    agent_id: str = ""
    model_id: str = ""
    input_text: str = ""
    output_text: str = ""
    retrieved_facts: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: dict[str, int] = Field(default_factory=dict)
    content_hash: str = ""
    signature: Optional[str] = None
    created_at: Optional[str] = None

    model_config = {"populate_by_name": True}


class ReplayResult(BaseModel):
    """Result of a decision replay."""
    status: str = ""  # identical | diverged | error
    confidence: float = 0.0
    original_output: str = ""
    replay_output: str = ""
    input_reconstructed: bool = False
    model_available: bool = False


# ── Erasure ───────────────────────────────────────────────────────────

class ErasureCertificate(BaseModel):
    """An Ed25519-signed erasure certificate."""
    certificate_id: str = ""
    tenant_id: str = ""
    fact_ref: Any = 0
    fact_content_hash: Optional[str] = None
    memory_deleted: bool = True
    decision_records_scrubbed: int = 0
    scrubbed_decision_ids: list[str] = Field(default_factory=list)
    erasure_requested_at: Optional[str] = None
    erasure_completed_at: Optional[str] = None
    legal_basis: str = ""
    requested_by: Optional[str] = None
    signature: Optional[str] = None
    public_key: Optional[str] = None
    verified: bool = False
    verification_note: Optional[str] = None

    model_config = {"populate_by_name": True}


class VerificationResult(BaseModel):
    """Result of an erasure certificate verification."""
    valid: bool = False
    detail: str = ""
    certificate_id: str = ""


# ── Orchestrator ──────────────────────────────────────────────────────

class Agent(BaseModel):
    """An agent definition."""
    id: str = Field(default="", alias="agent_id")
    name: str = ""
    role: str = ""
    model_id: str = ""
    system_prompt: str = ""
    tools: list[str] = Field(default_factory=list)
    store_id: Optional[str] = None
    created_at: Optional[str] = None

    model_config = {"populate_by_name": True}


class Step(BaseModel):
    """A workflow execution step."""
    step_index: int = 0
    agent_id: str = ""
    status: str = ""
    input_text: str = ""
    output_text: str = ""
    token_usage: dict[str, int] = Field(default_factory=dict)
    decision_id: str = ""
    tools_called: list[str] = Field(default_factory=list)


class Workflow(BaseModel):
    """A workflow definition."""
    id: str = Field(default="", alias="workflow_id")
    name: str = ""
    status: str = ""
    agents: list[str] = Field(default_factory=list)
    mode: str = "sequential"
    steps: list[Step] = Field(default_factory=list)
    created_at: Optional[str] = None

    model_config = {"populate_by_name": True}


class WorkflowRun(BaseModel):
    """Result of running or resuming a workflow."""
    workflow_id: str = ""
    status: str = ""
    steps: list[Step] = Field(default_factory=list)
    total_tokens: int = 0


class Receipt(BaseModel):
    """An execution receipt in the hash chain."""
    receipt_id: str = ""
    workflow_id: str = ""
    step_index: int = 0
    input_hash: str = ""
    output_hash: str = ""
    previous_receipt_hash: Optional[str] = None
    signature: Optional[str] = None
    public_key: Optional[str] = None


class ChainVerification(BaseModel):
    """Result of a hash chain verification."""
    status: str = ""  # intact | tampered
    steps_verified: int = 0
    tampered_at: Optional[int] = None


class OrchestratorStats(BaseModel):
    """Orchestrator summary statistics."""
    total_agents: int = 0
    total_workflows: int = 0
    total_steps: int = 0
    completed_workflows: int = 0


# ── Reports ───────────────────────────────────────────────────────────

class ReportSection(BaseModel):
    """A section within a regulatory report."""
    article: str = ""
    title: str = ""
    status: str = ""  # COMPLIANT | PARTIAL | INSUFFICIENT_DATA
    evidence: list[str] = Field(default_factory=list)
    detail: str = ""


class Report(BaseModel):
    """A generated regulatory compliance report."""
    id: str = Field(default="", alias="report_id")
    framework: str = ""
    generated_at: Optional[str] = None
    sections: list[ReportSection] = Field(default_factory=list)
    content_hash: Optional[str] = None
    signature: Optional[str] = None

    model_config = {"populate_by_name": True}


# ── LLM & Tools ──────────────────────────────────────────────────────

class LLMProvider(BaseModel):
    """A registered LLM provider."""
    model_id: str = ""
    provider: str = ""
    enabled: bool = True
    default_temperature: float = 0.7
    max_tokens: int = 4096


class ToolDefinition(BaseModel):
    """A registered tool definition."""
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    builtin: bool = False


# ── Portal ────────────────────────────────────────────────────────────

class Tenant(BaseModel):
    """A tenant account (returned from signup/login)."""
    token: str = ""
    tenant_id: str = ""
    name: str = ""
    email: str = ""
    api_key: str = ""
    plan: str = "starter"


class Session(BaseModel):
    """A portal login session (alias for Tenant)."""
    token: str = ""
    tenant_id: str = ""
    name: str = ""
    email: str = ""
    api_key: str = ""
    plan: str = "starter"


# ── Streaming ─────────────────────────────────────────────────────────

class StreamEvent(BaseModel):
    """A real-time event from SSE workflow streaming.

    Yielded by :meth:`OrchestratorService.stream_workflow`.
    """

    event: str
    """Event type (e.g. ``"step.governance_pass"``, ``"workflow.complete"``)."""

    data: dict[str, Any] = Field(default_factory=dict)
    """JSON payload with event-specific fields."""

    @property
    def step_index(self) -> int | None:
        """Step index (0-based), if this is a step-level event."""
        return self.data.get("step_index")

    @property
    def agent_name(self) -> str | None:
        """Agent name, if this is a step-level event."""
        return self.data.get("agent_name")

    @property
    def elapsed_ms(self) -> int | None:
        """Milliseconds since workflow start."""
        return self.data.get("elapsed_ms")

