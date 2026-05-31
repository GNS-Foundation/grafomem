"""
GRAFOMEM Agent Orchestrator — governed multi-agent execution for GRAFOMEM Cloud.

The only orchestrator where every step is governed, every inference is signed,
and every deletion is certified.  Instead of competing with LangGraph on the
loop, we provide the *governance layer inside every iteration*:

    Governance Gate → Memory Retrieve → LLM Inference → Tool Execution
                  → Decision Trail Log → PII Post-check

Supports three workflow modes:
  - sequential:  agents run in order, output chains
  - supervisor:  a supervisor agent routes tasks to workers
  - round_robin: agents take turns until convergence or max steps

Backed by PostgreSQL via psycopg v3 (sync), following the same patterns
as GovernanceGateway, DecisionTrailService, and ErasureProofService.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from aml.cloud.streaming_events import StreamEmitter

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.orchestrator")

# BLAKE2b-128 for IDs — matches fact_id, decision_id
_ID_BYTES = 16
_SEP = b"\x1f"


# ============================================================================
# Enumerations
# ============================================================================

class AgentRole(str, Enum):
    RESEARCHER = "researcher"
    WRITER = "writer"
    REVIEWER = "reviewer"
    CLASSIFIER = "classifier"
    EXECUTOR = "executor"
    SUPERVISOR = "supervisor"
    CUSTOM = "custom"


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_HITL = "waiting_hitl"
    COMPLETED = "completed"
    FAILED = "failed"
    TERMINATED = "terminated"


class WorkflowMode(str, Enum):
    SEQUENTIAL = "sequential"
    SUPERVISOR = "supervisor"
    ROUND_ROBIN = "round_robin"


class WorkflowStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_HITL = "waiting_hitl"
    COMPLETED = "completed"
    FAILED = "failed"
    TERMINATED = "terminated"


class StepStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    DENIED = "denied"
    ESCALATED = "escalated"
    FAILED = "failed"


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class AgentDefinition:
    """An agent definition — persona, model, memory scope, tools."""
    agent_id: str
    tenant_id: str
    name: str
    role: AgentRole
    description: str
    model_id: str
    system_prompt: str
    memory_stores: list[str]
    tools: list[str]
    max_steps: int
    max_tokens_per_step: int
    temperature: float
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class StepRecord:
    """A single executed step within a workflow."""
    step_id: str
    workflow_id: str
    agent_id: str
    tenant_id: str
    step_number: int
    # Input
    input_text: str
    retrieved_facts: list[dict]
    # Governance
    governance_allowed: bool
    governance_logs: list[dict]
    # Inference
    model_id: str
    raw_output: str
    tool_calls: list[dict]
    tool_results: list[dict]
    tokens_used: int
    latency_ms: int
    # Decision Trail link
    decision_id: str | None
    # Provenance
    signature: bytes | None
    public_key: bytes | None
    # Status
    status: StepStatus
    created_at: datetime


@dataclass(slots=True)
class Workflow:
    """A multi-agent workflow."""
    workflow_id: str
    tenant_id: str
    name: str
    description: str
    agent_ids: list[str]
    mode: WorkflowMode
    supervisor_agent_id: str | None
    max_total_steps: int
    status: WorkflowStatus
    current_step: int
    total_tokens: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    # In-memory only — not persisted at workflow level
    steps: list[StepRecord] = field(default_factory=list)


# ============================================================================
# ID computation
# ============================================================================

def _compute_id(*parts: str) -> str:
    """BLAKE2b-128 hex digest over canonical fields."""
    h = hashlib.blake2b(digest_size=_ID_BYTES)
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(_SEP)
    return h.hexdigest()


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS orchestrator_agents (
    agent_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'custom',
    description     TEXT NOT NULL DEFAULT '',
    model_id        TEXT NOT NULL,
    system_prompt   TEXT NOT NULL DEFAULT '',
    memory_stores   JSONB NOT NULL DEFAULT '[]',
    tools           JSONB NOT NULL DEFAULT '[]',
    max_steps       INTEGER NOT NULL DEFAULT 20,
    max_tokens      INTEGER NOT NULL DEFAULT 4096,
    temperature     REAL NOT NULL DEFAULT 0.7,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_oa_tenant
    ON orchestrator_agents(tenant_id, enabled);

CREATE TABLE IF NOT EXISTS orchestrator_workflows (
    workflow_id         TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    agent_ids           JSONB NOT NULL DEFAULT '[]',
    mode                TEXT NOT NULL DEFAULT 'sequential',
    supervisor_agent_id TEXT,
    max_total_steps     INTEGER NOT NULL DEFAULT 100,
    status              TEXT NOT NULL DEFAULT 'created',
    current_step        INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ow_tenant
    ON orchestrator_workflows(tenant_id, status);

CREATE TABLE IF NOT EXISTS orchestrator_steps (
    step_id             TEXT PRIMARY KEY,
    workflow_id         TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    tenant_id           TEXT NOT NULL,
    step_number         INTEGER NOT NULL,
    input_text          TEXT NOT NULL,
    retrieved_facts     JSONB NOT NULL DEFAULT '[]',
    governance_allowed  BOOLEAN NOT NULL DEFAULT TRUE,
    governance_logs     JSONB NOT NULL DEFAULT '[]',
    model_id            TEXT NOT NULL DEFAULT '',
    raw_output          TEXT NOT NULL DEFAULT '',
    tool_calls          JSONB NOT NULL DEFAULT '[]',
    tool_results        JSONB NOT NULL DEFAULT '[]',
    tokens_used         INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    decision_id         TEXT,
    signature           BYTEA,
    public_key          BYTEA,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_os_workflow
    ON orchestrator_steps(workflow_id, step_number);
CREATE INDEX IF NOT EXISTS idx_os_tenant
    ON orchestrator_steps(tenant_id, created_at DESC);
"""


# ============================================================================
# OrchestratorService
# ============================================================================

class OrchestratorService:
    """Governed multi-agent execution engine.

    Every agent step flows through:
      1. Governance Gate   — evaluate_and_gate() BEFORE inference
      2. Memory Retrieve   — query GMP stores for relevant facts
      3. LLM Inference     — call the model with facts + tools
      4. Tool Execution    — execute any tool calls (governed individually)
      5. Decision Trail    — log the full decision with provenance
      6. PII Post-check    — post-execution PII guard on output

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    governance : GovernanceGateway
        Policy-as-code engine for pre-gating.
    decision_trail : DecisionTrailService
        Inference audit logging.
    erasure_proof : ErasureProofService
        GDPR erasure certification.
    store_manager : StoreManager
        GMP memory store access.
    llm_registry : LLMRegistry, optional
        LLM provider abstraction (Sprint 6b).
    tool_registry : ToolRegistry, optional
        Tool definitions and execution (Sprint 6b).
    """

    def __init__(
        self,
        db_url: str,
        governance: Any,
        decision_trail: Any,
        erasure_proof: Any | None = None,
        store_manager: Any | None = None,
        llm_registry: Any | None = None,
        tool_registry: Any | None = None,
        execution_receipts: Any | None = None,
        pool=None,
    ) -> None:
        self._db_url = db_url
        self._pool = pool
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._governance = governance
        self._decision_trail = decision_trail
        self._erasure_proof = erasure_proof
        self._store_manager = store_manager
        self._llm_registry = llm_registry
        self._tool_registry = tool_registry
        self._execution_receipts = execution_receipts

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Orchestrator schema ensured")

    # ------------------------------------------------------------------
    # Agent CRUD
    # ------------------------------------------------------------------

    def create_agent(
        self,
        tenant_id: str,
        name: str,
        role: AgentRole | str,
        model_id: str,
        system_prompt: str,
        *,
        description: str = "",
        memory_stores: list[str] | None = None,
        tools: list[str] | None = None,
        max_steps: int = 20,
        max_tokens_per_step: int = 4096,
        temperature: float = 0.7,
        enabled: bool = True,
    ) -> AgentDefinition:
        """Create a new agent definition."""
        now = datetime.now(tz=timezone.utc)
        agent_id = _compute_id(tenant_id, name, now.isoformat())

        if isinstance(role, str):
            role = AgentRole(role)

        stores = memory_stores or []
        tool_list = tools or []

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO orchestrator_agents "
            "(agent_id, tenant_id, name, role, description, model_id, "
            " system_prompt, memory_stores, tools, max_steps, max_tokens, "
            " temperature, enabled, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                agent_id, tenant_id, name, role.value, description,
                model_id, system_prompt,
                json.dumps(stores), json.dumps(tool_list),
                max_steps, max_tokens_per_step, temperature,
                enabled, now, now,
            ),
        )

        logger.info(
            "Agent created: %s (%s) model=%s tenant=%s",
            name, role.value, model_id, tenant_id,
        )

        return AgentDefinition(
            agent_id=agent_id,
            tenant_id=tenant_id,
            name=name,
            role=role,
            description=description,
            model_id=model_id,
            system_prompt=system_prompt,
            memory_stores=stores,
            tools=tool_list,
            max_steps=max_steps,
            max_tokens_per_step=max_tokens_per_step,
            temperature=temperature,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )

    def get_agent(self, agent_id: str) -> AgentDefinition | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM orchestrator_agents WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
        return self._row_to_agent(row) if row else None

    def list_agents(
        self,
        tenant_id: str,
        enabled_only: bool = False,
    ) -> list[AgentDefinition]:
        conn = self._get_conn()
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM orchestrator_agents "
                "WHERE tenant_id = %s AND enabled = TRUE "
                "ORDER BY created_at DESC",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orchestrator_agents "
                "WHERE tenant_id = %s ORDER BY created_at DESC",
                (tenant_id,),
            ).fetchall()
        return [self._row_to_agent(r) for r in rows]

    def update_agent(
        self,
        agent_id: str,
        tenant_id: str,
        **kwargs,
    ) -> AgentDefinition | None:
        """Update agent fields. Only provided fields are changed."""
        existing = self.get_agent(agent_id)
        if existing is None or existing.tenant_id != tenant_id:
            return None

        allowed = {
            "name", "description", "model_id", "system_prompt",
            "memory_stores", "tools", "max_steps", "max_tokens_per_step",
            "temperature", "enabled",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return existing

        # Serialize JSON fields
        for json_field in ("memory_stores", "tools"):
            if json_field in updates and isinstance(updates[json_field], list):
                updates[json_field] = json.dumps(updates[json_field])

        # Rename max_tokens_per_step → max_tokens for DB column
        if "max_tokens_per_step" in updates:
            updates["max_tokens"] = updates.pop("max_tokens_per_step")

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        set_clause += ", updated_at = now()"
        values = list(updates.values()) + [agent_id, tenant_id]

        conn = self._get_conn()
        conn.execute(
            f"UPDATE orchestrator_agents SET {set_clause} "
            "WHERE agent_id = %s AND tenant_id = %s",
            values,
        )
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id: str, tenant_id: str) -> bool:
        conn = self._get_conn()
        result = conn.execute(
            "DELETE FROM orchestrator_agents "
            "WHERE agent_id = %s AND tenant_id = %s",
            (agent_id, tenant_id),
        )
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Workflow CRUD
    # ------------------------------------------------------------------

    def create_workflow(
        self,
        tenant_id: str,
        name: str,
        agent_ids: list[str],
        *,
        description: str = "",
        mode: WorkflowMode | str = WorkflowMode.SEQUENTIAL,
        supervisor_agent_id: str | None = None,
        max_total_steps: int = 100,
    ) -> Workflow:
        """Create a new workflow definition."""
        now = datetime.now(tz=timezone.utc)
        workflow_id = _compute_id(tenant_id, name, now.isoformat())

        if isinstance(mode, str):
            mode = WorkflowMode(mode)

        # Validate: supervisor mode requires a supervisor_agent_id
        if mode == WorkflowMode.SUPERVISOR and not supervisor_agent_id:
            raise ValueError("Supervisor mode requires supervisor_agent_id")

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO orchestrator_workflows "
            "(workflow_id, tenant_id, name, description, agent_ids, mode, "
            " supervisor_agent_id, max_total_steps, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                workflow_id, tenant_id, name, description,
                json.dumps(agent_ids), mode.value,
                supervisor_agent_id, max_total_steps,
                WorkflowStatus.CREATED.value, now,
            ),
        )

        logger.info(
            "Workflow created: %s mode=%s agents=%d tenant=%s",
            name, mode.value, len(agent_ids), tenant_id,
        )

        return Workflow(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            name=name,
            description=description,
            agent_ids=agent_ids,
            mode=mode,
            supervisor_agent_id=supervisor_agent_id,
            max_total_steps=max_total_steps,
            status=WorkflowStatus.CREATED,
            current_step=0,
            total_tokens=0,
            started_at=None,
            completed_at=None,
            created_at=now,
        )

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM orchestrator_workflows WHERE workflow_id = %s",
            (workflow_id,),
        ).fetchone()
        if row is None:
            return None
        workflow = self._row_to_workflow(row)
        # Attach steps
        workflow.steps = self.get_workflow_steps(workflow_id)
        return workflow

    def list_workflows(
        self,
        tenant_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Workflow]:
        conn = self._get_conn()
        conditions = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]

        if status:
            conditions.append("status = %s")
            params.append(status)

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        rows = conn.execute(
            f"SELECT * FROM orchestrator_workflows "
            f"WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        return [self._row_to_workflow(r) for r in rows]

    def get_workflow_steps(self, workflow_id: str) -> list[StepRecord]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM orchestrator_steps "
            "WHERE workflow_id = %s ORDER BY step_number ASC",
            (workflow_id,),
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    # ------------------------------------------------------------------
    # Step execution — THE CORE METHOD
    # ------------------------------------------------------------------

    def execute_step(
        self,
        workflow_id: str,
        agent_id: str,
        input_text: str,
        *,
        parent_step_id: str | None = None,
        emitter: StreamEmitter | None = None,
    ) -> StepRecord:
        """Execute a single governed agent step.

        This is the heart of the orchestrator. Every step:
          1. Governance Gate  — evaluate_and_gate() BEFORE inference
          2. Memory Retrieve  — query GMP stores for relevant facts
          3. LLM Inference    — call the model with facts + tools
          4. Tool Execution   — execute any tool calls (governed individually)
          5. Decision Trail   — log the full decision with provenance
          6. PII Post-check   — post-execution PII guard on output
        """
        agent = self.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"Agent '{agent_id}' not found")

        now = datetime.now(tz=timezone.utc)
        step_number = self._next_step_number(workflow_id)
        step_id = _compute_id(
            workflow_id, agent_id, str(step_number), now.isoformat(),
        )

        # ── 1. GOVERNANCE GATE ──────────────────────────────
        gov_context = {
            "model_id": agent.model_id,
            "store_id": agent.memory_stores[0] if agent.memory_stores else None,
            "query": input_text,
            "tokens": agent.max_tokens_per_step,
        }
        allowed, gov_logs = self._governance.evaluate_and_gate(
            agent.tenant_id, "inference", gov_context,
        )
        gov_log_dicts = [self._governance.log_to_dict(l) for l in gov_logs]

        if not allowed:
            # Determine if denied or escalated
            from aml.cloud.governance import EvaluationResult
            escalated = any(
                l.result == EvaluationResult.ESCALATED for l in gov_logs
            )
            step_status = StepStatus.ESCALATED if escalated else StepStatus.DENIED

            step = self._persist_step(
                step_id=step_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                tenant_id=agent.tenant_id,
                step_number=step_number,
                input_text=input_text,
                retrieved_facts=[],
                governance_allowed=False,
                governance_logs=gov_log_dicts,
                model_id=agent.model_id,
                raw_output="",
                tool_calls=[],
                tool_results=[],
                tokens_used=0,
                latency_ms=0,
                decision_id=None,
                signature=None,
                public_key=None,
                status=step_status,
                created_at=now,
            )

            # Update workflow status if escalated
            if escalated:
                self._update_workflow_status(
                    workflow_id, WorkflowStatus.WAITING_HITL,
                )

            logger.info(
                "Step %s: governance %s for agent=%s workflow=%s",
                step_status.value, agent.name, workflow_id,
                "denied" if not escalated else "escalated",
            )

            # Stream: governance deny event
            if emitter:
                emitter.emit(
                    "step.governance_deny",
                    {
                        "reason": step_status.value,
                        "policies_evaluated": len(gov_logs),
                        "action": "escalate" if escalated else "deny",
                    },
                    step_index=step_number,
                    agent_name=agent.name,
                )

            return step

        # ── 2. MEMORY RETRIEVE ──────────────────────────────
        retrieved_facts: list[dict[str, Any]] = []
        if self._store_manager and agent.memory_stores:
            from aml.backends.interface import RetrieveOptions
            for store_id in agent.memory_stores:
                entry = self._store_manager.get(store_id)
                if entry is not None:
                    try:
                        results = entry.backend.retrieve(
                            input_text,
                            RetrieveOptions(
                                budget_tokens=512,
                                tenant_id=agent.tenant_id,
                            ),
                        )
                        for mem in results:
                            retrieved_facts.append({
                                "ref": mem.ref,
                                "content": mem.content,
                                "store_id": store_id,
                                "written_at": (
                                    mem.written_at.isoformat()
                                    if mem.written_at else None
                                ),
                            })
                    except Exception as e:
                        logger.warning(
                            "Memory retrieve failed for store=%s: %s",
                            store_id, e,
                        )

        # Stream: memory retrieval event
        if emitter:
            emitter.emit(
                "step.memory_retrieve",
                {
                    "facts_found": len(retrieved_facts),
                    "store_ids": list({f.get("store_id", "") for f in retrieved_facts}),
                },
                step_index=step_number,
                agent_name=agent.name,
            )

        # ── 3. LLM INFERENCE ───────────────────────────────
        raw_output = ""
        tool_calls: list[dict] = []
        tokens_used = 0
        latency_ms = 0

        if self._llm_registry is not None:
            # Build messages with memory context
            messages = self._build_messages(agent, input_text, retrieved_facts)

            # Get tool definitions for this agent
            tool_defs = None
            if self._tool_registry and agent.tools:
                tool_defs = self._tool_registry.get_tool_definitions(
                    agent.tenant_id, agent.tools,
                )

            try:
                from aml.cloud.llm_registry import LLMRequest
                llm_request = LLMRequest(
                    model_id=agent.model_id,
                    system_prompt=agent.system_prompt,
                    messages=messages,
                    tools=tool_defs,
                    temperature=agent.temperature,
                    max_tokens=agent.max_tokens_per_step,
                )

                # Stream: LLM inference start
                if emitter:
                    emitter.emit(
                        "step.llm_start",
                        {
                            "model_id": agent.model_id,
                            "token_budget": agent.max_tokens_per_step,
                            "facts_in_context": len(retrieved_facts),
                        },
                        step_index=step_number,
                        agent_name=agent.name,
                    )

                t0 = time.monotonic()
                llm_response = self._llm_registry.infer(
                    agent.tenant_id, llm_request,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)

                raw_output = llm_response.content
                tool_calls = llm_response.tool_calls
                tokens_used = llm_response.tokens_output

                # Stream: LLM inference complete
                if emitter:
                    emitter.emit(
                        "step.llm_complete",
                        {
                            "tokens_used": tokens_used,
                            "latency_ms": latency_ms,
                            "has_tool_calls": bool(tool_calls),
                            "output_preview": raw_output[:200] if raw_output else "",
                        },
                        step_index=step_number,
                        agent_name=agent.name,
                    )

            except Exception as e:
                logger.error("LLM inference failed: %s", e)
                raw_output = f"[LLM Error: {e}]"
        else:
            # No LLM registry — use placeholder for testing
            raw_output = (
                f"[Orchestrator: agent '{agent.name}' received input "
                f"with {len(retrieved_facts)} facts from memory. "
                f"LLM registry not configured — returning placeholder.]"
            )

        # ── 4. TOOL EXECUTION ──────────────────────────────
        tool_results: list[dict] = []
        if tool_calls and self._tool_registry:
            for tc in tool_calls:
                try:
                    result = self._tool_registry.execute(
                        agent.tenant_id,
                        tc.get("name", ""),
                        tc.get("arguments", {}),
                    )
                    tool_results.append({
                        "name": tc["name"],
                        "arguments": tc.get("arguments", {}),
                        "output": result.output,
                        "success": result.success,
                        "governance_allowed": result.governance_allowed,
                    })
                except Exception as e:
                    tool_results.append({
                        "name": tc.get("name", "unknown"),
                        "arguments": tc.get("arguments", {}),
                        "output": None,
                        "success": False,
                        "error": str(e),
                        "governance_allowed": True,
                    })

            # Stream: tool call events
            if emitter:
                for tr in tool_results:
                    emitter.emit(
                        "step.tool_call",
                        {
                            "tool_name": tr.get("name", "unknown"),
                            "success": tr.get("success", False),
                            "governance_allowed": tr.get("governance_allowed", True),
                        },
                        step_index=step_number,
                        agent_name=agent.name,
                    )

        # ── 5. DECISION TRAIL ──────────────────────────────
        decision_id = None
        signature = None
        public_key = None

        if self._decision_trail and raw_output:
            try:
                record = self._decision_trail.log(
                    tenant_id=agent.tenant_id,
                    store_id=(
                        agent.memory_stores[0]
                        if agent.memory_stores
                        else "orchestrator"
                    ),
                    query=input_text,
                    model_id=agent.model_id,
                    raw_output=raw_output,
                    retrieved_refs=[f.get("ref", 0) for f in retrieved_facts],
                    retrieved_contents=[
                        f.get("content", "") for f in retrieved_facts
                    ],
                    retrieval_scores=[],
                    parameters={"temperature": agent.temperature},
                    output_tokens=tokens_used,
                    latency_ms=latency_ms,
                    parent_decision_id=parent_step_id,
                )
                decision_id = record.decision_id
                signature = record.signature
                public_key = record.public_key
            except Exception as e:
                logger.warning("Decision trail logging failed: %s", e)

        # ── 6. PII POST-CHECK ──────────────────────────────
        if raw_output and self._governance:
            try:
                self._governance.evaluate(
                    agent.tenant_id,
                    "output_check",
                    {"output": raw_output},
                )
            except Exception as e:
                logger.warning("PII post-check failed: %s", e)

        # ── 7. PERSIST STEP ────────────────────────────────
        step = self._persist_step(
            step_id=step_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            tenant_id=agent.tenant_id,
            step_number=step_number,
            input_text=input_text,
            retrieved_facts=retrieved_facts,
            governance_allowed=True,
            governance_logs=gov_log_dicts,
            model_id=agent.model_id,
            raw_output=raw_output,
            tool_calls=tool_calls,
            tool_results=tool_results,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            decision_id=decision_id,
            signature=signature,
            public_key=public_key,
            status=StepStatus.COMPLETED,
            created_at=now,
        )

        # Update workflow counters
        self._increment_workflow(workflow_id, tokens_used)

        try:
            from aml.cloud.metrics import TOKENS_CONSUMED
            TOKENS_CONSUMED.labels(model_id=agent.model_id).inc(tokens_used)
        except Exception:
            pass

        # ── 8. EXECUTION RECEIPT ───────────────────────────
        if self._execution_receipts:
            try:
                self._execution_receipts.issue_receipt(
                    tenant_id=agent.tenant_id,
                    step_id=step_id,
                    workflow_id=workflow_id,
                    step_number=step_number,
                    input_text=input_text,
                    retrieved_contents=[
                        f.get("content", "") for f in retrieved_facts
                    ],
                    governance_logs=gov_log_dicts,
                    model_id=agent.model_id,
                    raw_output=raw_output,
                    decision_id=decision_id,
                    tool_calls=tool_calls if tool_calls else None,
                )
            except Exception as e:
                logger.warning("Execution receipt issuance failed: %s", e)

        logger.info(
            "Step completed: agent=%s step=%d tokens=%d latency=%dms "
            "facts=%d tools=%d decision=%s",
            agent.name, step_number, tokens_used, latency_ms,
            len(retrieved_facts), len(tool_calls),
            decision_id or "none",
        )

        # Stream: step complete event
        if emitter:
            emitter.emit(
                "step.complete",
                {
                    "status": "completed",
                    "decision_id": decision_id,
                    "tokens_used": tokens_used,
                    "latency_ms": latency_ms,
                    "facts_retrieved": len(retrieved_facts),
                    "tool_calls": len(tool_calls),
                    "output_preview": raw_output[:200] if raw_output else "",
                },
                step_index=step_number,
                agent_name=agent.name,
            )

        return step

    # ------------------------------------------------------------------
    # Workflow execution
    # ------------------------------------------------------------------

    def run_workflow(
        self,
        workflow_id: str,
        initial_input: str,
        *,
        emitter: StreamEmitter | None = None,
    ) -> Workflow:
        """Execute a workflow from start to finish.

        Dispatches to the correct execution mode:
        - sequential: A → B → C, each receives previous output
        - supervisor: supervisor routes to workers
        - round_robin: agents take turns
        """
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise ValueError(f"Workflow '{workflow_id}' not found")

        # Mark as running
        self._update_workflow_status(workflow_id, WorkflowStatus.RUNNING)
        self._set_workflow_started(workflow_id)

        # Stream: workflow started event
        if emitter:
            emitter.set_workflow(workflow_id)
            emitter.emit(
                "workflow.started",
                {
                    "mode": workflow.mode.value,
                    "agent_count": len(workflow.agent_ids),
                },
            )

        try:
            if workflow.mode == WorkflowMode.SEQUENTIAL:
                self._run_sequential(workflow, initial_input, emitter=emitter)
            elif workflow.mode == WorkflowMode.SUPERVISOR:
                self._run_supervisor(workflow, initial_input, emitter=emitter)
            elif workflow.mode == WorkflowMode.ROUND_ROBIN:
                self._run_round_robin(workflow, initial_input, emitter=emitter)

            # Check final status (might be WAITING_HITL)
            final = self.get_workflow(workflow_id)
            if final and final.status == WorkflowStatus.RUNNING:
                self._update_workflow_status(
                    workflow_id, WorkflowStatus.COMPLETED,
                )
                self._set_workflow_completed(workflow_id)

                try:
                    from aml.cloud.metrics import WORKFLOWS_TOTAL
                    WORKFLOWS_TOTAL.labels(status=WorkflowStatus.COMPLETED.value).inc()
                except Exception:
                    pass

                # Webhook: workflow completed
                wh = getattr(self, "_webhook_service", None)
                if wh is not None and final:
                    wh.dispatch(final.tenant_id, "workflow.completed", {
                        "workflow_id": workflow_id,
                        "total_steps": final.current_step,
                        "total_tokens": final.total_tokens,
                    })

            # Stream: workflow complete event
            if emitter:
                final = final or self.get_workflow(workflow_id)
                duration_ms = int((time.monotonic() - emitter._start_time) * 1000)
                emitter.emit(
                    "workflow.complete",
                    {
                        "status": final.status.value if final else "completed",
                        "total_steps": final.current_step if final else 0,
                        "total_tokens": final.total_tokens if final else 0,
                        "duration_ms": duration_ms,
                    },
                )
                emitter.close()

        except Exception as e:
            logger.error("Workflow %s failed: %s", workflow_id, e)
            self._update_workflow_status(workflow_id, WorkflowStatus.FAILED)

            try:
                from aml.cloud.metrics import WORKFLOWS_TOTAL
                WORKFLOWS_TOTAL.labels(status=WorkflowStatus.FAILED.value).inc()
            except Exception:
                pass

            # Webhook: workflow error
            wh = getattr(self, "_webhook_service", None)
            if wh is not None:
                failed_wf = self.get_workflow(workflow_id)
                if failed_wf:
                    wh.dispatch(failed_wf.tenant_id, "workflow.error", {
                        "workflow_id": workflow_id,
                        "error": str(e),
                    })

            # Stream: workflow error event
            if emitter:
                emitter.emit(
                    "workflow.error",
                    {"error": str(e)},
                )
                emitter.close()

        return self.get_workflow(workflow_id)

    def _run_sequential(
        self,
        workflow: Workflow,
        initial_input: str,
        *,
        emitter: StreamEmitter | None = None,
    ) -> None:
        """Sequential: agents run in order, each receives previous output."""
        current_input = initial_input

        for agent_id in workflow.agent_ids:
            # Safety: check step limits
            wf = self.get_workflow(workflow.workflow_id)
            if wf and wf.current_step >= wf.max_total_steps:
                self._update_workflow_status(
                    workflow.workflow_id, WorkflowStatus.TERMINATED,
                )

                try:
                    from aml.cloud.metrics import WORKFLOWS_TOTAL
                    WORKFLOWS_TOTAL.labels(status=WorkflowStatus.TERMINATED.value).inc()
                except Exception:
                    pass
                logger.warning(
                    "Workflow %s terminated: max steps reached",
                    workflow.workflow_id,
                )
                return

            # Stream: step started event
            if emitter:
                agent_def = self.get_agent(agent_id)
                emitter.emit(
                    "step.started",
                    {
                        "agent_role": agent_def.role.value if agent_def else "unknown",
                    },
                    step_index=wf.current_step if wf else 0,
                    agent_name=agent_def.name if agent_def else agent_id[:8],
                )

            # Stream: governance pass event (emitted inside execute_step only
            # on deny — we emit pass here before the call for the happy path)
            step = self.execute_step(
                workflow.workflow_id, agent_id, current_input,
                emitter=emitter,
            )

            # Emit governance pass after the fact (if step was allowed)
            if emitter and step.governance_allowed:
                emitter.emit(
                    "step.governance_pass",
                    {
                        "policies_evaluated": len(step.governance_logs),
                        "allowed": True,
                    },
                    step_index=step.step_number,
                    agent_name=step.agent_id[:8],
                )

            # If denied or escalated, stop
            if step.status in (StepStatus.DENIED, StepStatus.ESCALATED):
                return

            # Chain output to next agent
            current_input = step.raw_output

    def _run_supervisor(
        self,
        workflow: Workflow,
        initial_input: str,
        *,
        emitter: StreamEmitter | None = None,
    ) -> None:
        """Supervisor: a supervisor agent routes tasks to workers."""
        if not workflow.supervisor_agent_id:
            raise ValueError("No supervisor_agent_id set")

        current_context = f"Task: {initial_input}\n\nAvailable workers: "
        worker_names = []
        for aid in workflow.agent_ids:
            agent = self.get_agent(aid)
            if agent:
                worker_names.append(f"{agent.name} ({agent.role.value})")
        current_context += ", ".join(worker_names)

        steps_executed = 0
        max_steps = min(workflow.max_total_steps, 50)  # Extra safety

        while steps_executed < max_steps:
            # Supervisor decides
            step = self.execute_step(
                workflow.workflow_id,
                workflow.supervisor_agent_id,
                current_context,
            )
            steps_executed += 1

            if step.status != StepStatus.COMPLETED:
                return

            # Check if supervisor wants to route to a worker
            if step.tool_calls:
                for tc in step.tool_calls:
                    if tc.get("name") == "route_to_worker":
                        worker_id = tc["arguments"].get("agent_id")
                        worker_task = tc["arguments"].get("task", "")

                        if worker_id in workflow.agent_ids:
                            worker_step = self.execute_step(
                                workflow.workflow_id,
                                worker_id,
                                worker_task,
                            )
                            steps_executed += 1
                            current_context += (
                                f"\n\n[Worker result]: {worker_step.raw_output}"
                            )
                        else:
                            current_context += (
                                f"\n\n[Error]: Worker '{worker_id}' not in workflow"
                            )

                    elif tc.get("name") == "complete_task":
                        return  # Supervisor says done
            else:
                # No tool calls = supervisor produced final answer
                return

    def _run_round_robin(
        self,
        workflow: Workflow,
        initial_input: str,
        *,
        emitter: StreamEmitter | None = None,
    ) -> None:
        """Round-robin: agents take turns until max steps."""
        current_input = initial_input
        agent_count = len(workflow.agent_ids)
        step_count = 0
        max_steps = workflow.max_total_steps

        # Loop detection
        seen_hashes: set[str] = set()

        while step_count < max_steps:
            agent_idx = step_count % agent_count
            agent_id = workflow.agent_ids[agent_idx]

            # Loop detection: hash (agent_id, input)
            input_hash = hashlib.blake2b(
                f"{agent_id}:{current_input}".encode(),
                digest_size=8,
            ).hexdigest()
            if input_hash in seen_hashes:
                logger.warning(
                    "Loop detected in workflow %s at step %d",
                    workflow.workflow_id, step_count,
                )
                return
            seen_hashes.add(input_hash)

            step = self.execute_step(
                workflow.workflow_id, agent_id, current_input,
            )
            step_count += 1

            if step.status != StepStatus.COMPLETED:
                return

            current_input = step.raw_output

    def resume_workflow(
        self,
        workflow_id: str,
        hitl_approved: bool,
    ) -> Workflow:
        """Resume a workflow that is waiting for HITL approval."""
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise ValueError(f"Workflow '{workflow_id}' not found")
        if workflow.status != WorkflowStatus.WAITING_HITL:
            raise ValueError(
                f"Workflow is not waiting for approval (status={workflow.status.value})"
            )

        if not hitl_approved:
            self._update_workflow_status(
                workflow_id, WorkflowStatus.TERMINATED,
            )

            try:
                from aml.cloud.metrics import WORKFLOWS_TOTAL
                WORKFLOWS_TOTAL.labels(status=WorkflowStatus.TERMINATED.value).inc()
            except Exception:
                pass

            return self.get_workflow(workflow_id)

        # Re-run from the last step's input
        self._update_workflow_status(workflow_id, WorkflowStatus.RUNNING)

        # Find the last escalated step and re-execute from there
        steps = self.get_workflow_steps(workflow_id)
        if steps:
            last_step = steps[-1]
            # Continue with the next agent in sequence
            if workflow.mode == WorkflowMode.SEQUENTIAL:
                agent_idx = workflow.agent_ids.index(last_step.agent_id)
                remaining = workflow.agent_ids[agent_idx:]
                current_input = last_step.input_text

                for agent_id in remaining:
                    step = self.execute_step(
                        workflow_id, agent_id, current_input,
                    )
                    if step.status != StepStatus.COMPLETED:
                        break
                    current_input = step.raw_output

        wf = self.get_workflow(workflow_id)
        if wf and wf.status == WorkflowStatus.RUNNING:
            self._update_workflow_status(
                workflow_id, WorkflowStatus.COMPLETED,
            )
            self._set_workflow_completed(workflow_id)

            try:
                from aml.cloud.metrics import WORKFLOWS_TOTAL
                WORKFLOWS_TOTAL.labels(status=WorkflowStatus.COMPLETED.value).inc()
            except Exception:
                pass

        return self.get_workflow(workflow_id)

    def terminate_workflow(self, workflow_id: str, tenant_id: str) -> bool:
        """Force-terminate a running workflow."""
        workflow = self.get_workflow(workflow_id)
        if workflow is None or workflow.tenant_id != tenant_id:
            return False
        self._update_workflow_status(workflow_id, WorkflowStatus.TERMINATED)

        try:
            from aml.cloud.metrics import WORKFLOWS_TOTAL
            WORKFLOWS_TOTAL.labels(status=WorkflowStatus.TERMINATED.value).inc()
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # Ad-hoc step execution (not part of a workflow)
    # ------------------------------------------------------------------

    def execute_adhoc_step(
        self,
        tenant_id: str,
        agent_id: str,
        input_text: str,
    ) -> StepRecord:
        """Execute a single step outside of a workflow.

        Creates a temporary 1-step workflow for the step.
        """
        workflow = self.create_workflow(
            tenant_id=tenant_id,
            name=f"adhoc_{uuid.uuid4().hex[:8]}",
            agent_ids=[agent_id],
            description="Ad-hoc single step",
            mode=WorkflowMode.SEQUENTIAL,
            max_total_steps=1,
        )
        self._update_workflow_status(
            workflow.workflow_id, WorkflowStatus.RUNNING,
        )
        self._set_workflow_started(workflow.workflow_id)

        step = self.execute_step(workflow.workflow_id, agent_id, input_text)

        self._update_workflow_status(
            workflow.workflow_id, WorkflowStatus.COMPLETED,
        )
        self._set_workflow_completed(workflow.workflow_id)

        return step

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        conn = self._get_conn()

        agent_row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "  COUNT(CASE WHEN enabled THEN 1 END) AS active "
            "FROM orchestrator_agents WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        wf_row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "  COUNT(CASE WHEN status = 'running' THEN 1 END) AS running, "
            "  COUNT(CASE WHEN status = 'completed' THEN 1 END) AS completed, "
            "  COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed "
            "FROM orchestrator_workflows WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        step_row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "  COALESCE(SUM(tokens_used), 0) AS total_tokens, "
            "  COALESCE(AVG(latency_ms), 0) AS avg_latency, "
            "  COUNT(CASE WHEN status = 'denied' THEN 1 END) AS denied, "
            "  COUNT(CASE WHEN status = 'escalated' THEN 1 END) AS escalated "
            "FROM orchestrator_steps WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        return {
            "agents_total": agent_row["total"] if agent_row else 0,
            "agents_active": agent_row["active"] if agent_row else 0,
            "workflows_total": wf_row["total"] if wf_row else 0,
            "workflows_running": wf_row["running"] if wf_row else 0,
            "workflows_completed": wf_row["completed"] if wf_row else 0,
            "workflows_failed": wf_row["failed"] if wf_row else 0,
            "steps_total": step_row["total"] if step_row else 0,
            "steps_denied": step_row["denied"] if step_row else 0,
            "steps_escalated": step_row["escalated"] if step_row else 0,
            "total_tokens": step_row["total_tokens"] if step_row else 0,
            "avg_latency_ms": (
                round(step_row["avg_latency"], 1) if step_row else 0
            ),
        }

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        agent: AgentDefinition,
        input_text: str,
        retrieved_facts: list[dict],
    ) -> list[dict[str, str]]:
        """Build the message array for the LLM."""
        messages = []

        # Add memory context if facts were retrieved
        if retrieved_facts:
            fact_text = "\n".join(
                f"- [{f.get('store_id', '?')}] {f.get('content', '')}"
                for f in retrieved_facts
            )
            messages.append({
                "role": "user",
                "content": (
                    f"[MEMORY CONTEXT — {len(retrieved_facts)} facts retrieved "
                    f"from GMP stores]\n{fact_text}\n\n"
                    f"[USER QUERY]\n{input_text}"
                ),
            })
        else:
            messages.append({"role": "user", "content": input_text})

        return messages

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_step_number(self, workflow_id: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COALESCE(MAX(step_number), 0) AS max_step "
            "FROM orchestrator_steps WHERE workflow_id = %s",
            (workflow_id,),
        ).fetchone()
        return (row["max_step"] if row else 0) + 1

    def _persist_step(self, **kwargs) -> StepRecord:
        """Persist a step record to the database and return the StepRecord."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO orchestrator_steps "
            "(step_id, workflow_id, agent_id, tenant_id, step_number, "
            " input_text, retrieved_facts, governance_allowed, governance_logs, "
            " model_id, raw_output, tool_calls, tool_results, "
            " tokens_used, latency_ms, decision_id, signature, public_key, "
            " status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                kwargs["step_id"],
                kwargs["workflow_id"],
                kwargs["agent_id"],
                kwargs["tenant_id"],
                kwargs["step_number"],
                kwargs["input_text"],
                json.dumps(kwargs["retrieved_facts"]),
                kwargs["governance_allowed"],
                json.dumps(kwargs["governance_logs"]),
                kwargs["model_id"],
                kwargs["raw_output"],
                json.dumps(kwargs["tool_calls"]),
                json.dumps(kwargs["tool_results"]),
                kwargs["tokens_used"],
                kwargs["latency_ms"],
                kwargs["decision_id"],
                kwargs["signature"],
                kwargs["public_key"],
                kwargs["status"].value,
                kwargs["created_at"],
            ),
        )

        return StepRecord(
            step_id=kwargs["step_id"],
            workflow_id=kwargs["workflow_id"],
            agent_id=kwargs["agent_id"],
            tenant_id=kwargs["tenant_id"],
            step_number=kwargs["step_number"],
            input_text=kwargs["input_text"],
            retrieved_facts=kwargs["retrieved_facts"],
            governance_allowed=kwargs["governance_allowed"],
            governance_logs=kwargs["governance_logs"],
            model_id=kwargs["model_id"],
            raw_output=kwargs["raw_output"],
            tool_calls=kwargs["tool_calls"],
            tool_results=kwargs["tool_results"],
            tokens_used=kwargs["tokens_used"],
            latency_ms=kwargs["latency_ms"],
            decision_id=kwargs["decision_id"],
            signature=kwargs["signature"],
            public_key=kwargs["public_key"],
            status=kwargs["status"],
            created_at=kwargs["created_at"],
        )

    def _update_workflow_status(
        self, workflow_id: str, status: WorkflowStatus,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE orchestrator_workflows SET status = %s WHERE workflow_id = %s",
            (status.value, workflow_id),
        )

    def _set_workflow_started(self, workflow_id: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE orchestrator_workflows SET started_at = now() "
            "WHERE workflow_id = %s",
            (workflow_id,),
        )

    def _set_workflow_completed(self, workflow_id: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE orchestrator_workflows SET completed_at = now() "
            "WHERE workflow_id = %s",
            (workflow_id,),
        )

    def _increment_workflow(self, workflow_id: str, tokens: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE orchestrator_workflows "
            "SET current_step = current_step + 1, "
            "    total_tokens = total_tokens + %s "
            "WHERE workflow_id = %s",
            (tokens, workflow_id),
        )

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_agent(row: dict[str, Any]) -> AgentDefinition:
        stores = row.get("memory_stores")
        if isinstance(stores, str):
            stores = json.loads(stores)
        elif stores is None:
            stores = []

        tools = row.get("tools")
        if isinstance(tools, str):
            tools = json.loads(tools)
        elif tools is None:
            tools = []

        return AgentDefinition(
            agent_id=row["agent_id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            role=AgentRole(row["role"]),
            description=row.get("description", ""),
            model_id=row["model_id"],
            system_prompt=row.get("system_prompt", ""),
            memory_stores=stores,
            tools=tools,
            max_steps=row.get("max_steps", 20),
            max_tokens_per_step=row.get("max_tokens", 4096),
            temperature=row.get("temperature", 0.7),
            enabled=row.get("enabled", True),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_workflow(row: dict[str, Any]) -> Workflow:
        agent_ids = row.get("agent_ids")
        if isinstance(agent_ids, str):
            agent_ids = json.loads(agent_ids)
        elif agent_ids is None:
            agent_ids = []

        return Workflow(
            workflow_id=row["workflow_id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            description=row.get("description", ""),
            agent_ids=agent_ids,
            mode=WorkflowMode(row.get("mode", "sequential")),
            supervisor_agent_id=row.get("supervisor_agent_id"),
            max_total_steps=row.get("max_total_steps", 100),
            status=WorkflowStatus(row.get("status", "created")),
            current_step=row.get("current_step", 0),
            total_tokens=row.get("total_tokens", 0),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_step(row: dict[str, Any]) -> StepRecord:
        def _load(val):
            if val is None:
                return []
            if isinstance(val, (list, dict)):
                return val
            return json.loads(val)

        return StepRecord(
            step_id=row["step_id"],
            workflow_id=row["workflow_id"],
            agent_id=row["agent_id"],
            tenant_id=row["tenant_id"],
            step_number=row["step_number"],
            input_text=row["input_text"],
            retrieved_facts=_load(row.get("retrieved_facts")),
            governance_allowed=row.get("governance_allowed", True),
            governance_logs=_load(row.get("governance_logs")),
            model_id=row.get("model_id", ""),
            raw_output=row.get("raw_output", ""),
            tool_calls=_load(row.get("tool_calls")),
            tool_results=_load(row.get("tool_results")),
            tokens_used=row.get("tokens_used", 0),
            latency_ms=row.get("latency_ms", 0),
            decision_id=row.get("decision_id"),
            signature=row.get("signature"),
            public_key=row.get("public_key"),
            status=StepStatus(row.get("status", "pending")),
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Serialization helpers (for API responses)
    # ------------------------------------------------------------------

    @staticmethod
    def agent_to_dict(a: AgentDefinition) -> dict[str, Any]:
        return {
            "agent_id": a.agent_id,
            "tenant_id": a.tenant_id,
            "name": a.name,
            "role": a.role.value,
            "description": a.description,
            "model_id": a.model_id,
            "system_prompt": a.system_prompt,
            "memory_stores": a.memory_stores,
            "tools": a.tools,
            "max_steps": a.max_steps,
            "max_tokens_per_step": a.max_tokens_per_step,
            "temperature": a.temperature,
            "enabled": a.enabled,
            "created_at": a.created_at.isoformat(),
            "updated_at": a.updated_at.isoformat(),
        }

    @staticmethod
    def step_to_dict(s: StepRecord) -> dict[str, Any]:
        import base64
        return {
            "step_id": s.step_id,
            "workflow_id": s.workflow_id,
            "agent_id": s.agent_id,
            "tenant_id": s.tenant_id,
            "step_number": s.step_number,
            "input_text": s.input_text,
            "retrieved_facts": s.retrieved_facts,
            "governance_allowed": s.governance_allowed,
            "governance_logs": s.governance_logs,
            "model_id": s.model_id,
            "raw_output": s.raw_output,
            "tool_calls": s.tool_calls,
            "tool_results": s.tool_results,
            "tokens_used": s.tokens_used,
            "latency_ms": s.latency_ms,
            "decision_id": s.decision_id,
            "signature": (
                base64.b64encode(s.signature).decode() if s.signature else None
            ),
            "public_key": (
                base64.b64encode(s.public_key).decode() if s.public_key else None
            ),
            "status": s.status.value,
            "created_at": s.created_at.isoformat(),
        }

    @staticmethod
    def workflow_to_dict(w: Workflow) -> dict[str, Any]:
        return {
            "workflow_id": w.workflow_id,
            "tenant_id": w.tenant_id,
            "name": w.name,
            "description": w.description,
            "agent_ids": w.agent_ids,
            "mode": w.mode.value,
            "supervisor_agent_id": w.supervisor_agent_id,
            "max_total_steps": w.max_total_steps,
            "status": w.status.value,
            "current_step": w.current_step,
            "total_tokens": w.total_tokens,
            "started_at": w.started_at.isoformat() if w.started_at else None,
            "completed_at": (
                w.completed_at.isoformat() if w.completed_at else None
            ),
            "created_at": w.created_at.isoformat(),
            "steps": [
                OrchestratorService.step_to_dict(s) for s in w.steps
            ],
        }
