"""
GRAFOMEM Tool Registry — governed tool definitions and execution.

Manages tool definitions (built-in + custom) and executes tool calls
with governance.  Every tool call optionally passes through the
Governance Gateway before execution — model allowlists, data scoping,
rate limits, and PII guards all apply to tools, not just LLM queries.

Built-in tools provide direct access to GMP memory operations:
  - grafomem_retrieve: query GMP stores for relevant facts
  - grafomem_write: write a new fact to a GMP store
  - grafomem_delete: delete a fact (triggers erasure proof)
  - grafomem_audit: get the full audit trail for a store

Custom tools are webhook-based: user defines a URL, method, headers,
and input schema.  The registry validates arguments against the JSON
schema and makes the HTTP call.

Backed by PostgreSQL via psycopg v3 (sync).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.tool_registry")


# ============================================================================
# Enumerations
# ============================================================================

class ToolType(str, Enum):
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    MEMORY_DELETE = "memory_delete"
    HTTP_REQUEST = "http_request"
    DATABASE_QUERY = "database_query"
    CUSTOM = "custom"


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class ToolDefinition:
    """A registered tool definition."""
    tool_id: str
    tenant_id: str
    name: str
    description: str
    tool_type: ToolType
    input_schema: dict[str, Any]
    config: dict[str, Any]
    enabled: bool
    requires_governance: bool
    is_builtin: bool
    created_at: datetime


@dataclass(slots=True)
class ToolResult:
    """Result of a tool execution."""
    tool_name: str
    success: bool
    output: Any
    error: str | None = None
    latency_ms: int = 0
    governance_allowed: bool = True
    governance_logs: list[dict] = field(default_factory=list)


# ============================================================================
# Built-in tool definitions
# ============================================================================

BUILTIN_TOOLS = [
    {
        "name": "grafomem_retrieve",
        "description": (
            "Retrieve relevant facts from a GRAFOMEM memory store. "
            "Use this to search for information in the agent's memory."
        ),
        "tool_type": ToolType.MEMORY_READ,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to find relevant facts",
                },
                "store_id": {
                    "type": "string",
                    "description": "The memory store to search",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max number of results to return",
                    "default": 10,
                },
            },
            "required": ["query", "store_id", "top_k"],
            "additionalProperties": False,
        },
        "config": {},
        "requires_governance": True,
    },
    {
        "name": "grafomem_write",
        "description": (
            "Write a new fact to a GRAFOMEM memory store. "
            "Use this to save new information the agent has learned."
        ),
        "tool_type": ToolType.MEMORY_WRITE,
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact content to store",
                },
                "store_id": {
                    "type": "string",
                    "description": "The memory store to write to",
                },
            },
            "required": ["content", "store_id"],
            "additionalProperties": False,
        },
        "config": {},
        "requires_governance": True,
    },
    {
        "name": "grafomem_delete",
        "description": (
            "Delete a fact from a GRAFOMEM memory store. "
            "This triggers an erasure certificate for GDPR compliance. "
            "Use with caution — deletion is irreversible."
        ),
        "tool_type": ToolType.MEMORY_DELETE,
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "integer",
                    "description": "The fact reference ID to delete",
                },
                "store_id": {
                    "type": "string",
                    "description": "The memory store containing the fact",
                },
            },
            "required": ["ref", "store_id"],
            "additionalProperties": False,
        },
        "config": {},
        "requires_governance": True,
    },
    {
        "name": "grafomem_audit",
        "description": (
            "Get the complete audit trail for a GRAFOMEM memory store. "
            "Returns all facts including their provenance metadata."
        ),
        "tool_type": ToolType.MEMORY_READ,
        "input_schema": {
            "type": "object",
            "properties": {
                "store_id": {
                    "type": "string",
                    "description": "The memory store to audit",
                },
            },
            "required": ["store_id"],
            "additionalProperties": False,
        },
        "config": {},
        "requires_governance": True,
    },
    {
        "name": "http_get",
        "description": "Make an HTTP GET request to fetch data from a URL.",
        "tool_type": ToolType.HTTP_REQUEST,
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch",
                },
                "headers": {
                    "type": "object",
                    "description": "Optional HTTP headers",
                    "default": {},
                },
            },
            "required": ["url", "headers"],
            "additionalProperties": False,
        },
        "config": {"method": "GET"},
        "requires_governance": True,
    },
    {
        "name": "http_post",
        "description": "Make an HTTP POST request to send data to a URL.",
        "tool_type": ToolType.HTTP_REQUEST,
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to send data to",
                },
                "body": {
                    "type": "object",
                    "description": "The JSON body to send",
                },
                "headers": {
                    "type": "object",
                    "description": "Optional HTTP headers",
                    "default": {},
                },
            },
            "required": ["url", "body", "headers"],
            "additionalProperties": False,
        },
        "config": {"method": "POST"},
        "requires_governance": True,
    },
]


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tool_definitions (
    tool_id             TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    tool_type           TEXT NOT NULL,
    input_schema        JSONB NOT NULL DEFAULT '{}',
    config              JSONB NOT NULL DEFAULT '{}',
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    requires_governance BOOLEAN NOT NULL DEFAULT TRUE,
    is_builtin          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, name)
);
CREATE INDEX IF NOT EXISTS idx_td_tenant
    ON tool_definitions(tenant_id, enabled);
"""


# ============================================================================
# ToolRegistry
# ============================================================================

class ToolRegistry:
    """Manages tool definitions and executes tool calls with governance.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    governance : GovernanceGateway
        Policy-as-code engine for tool call gating.
    store_manager : StoreManager
        GMP memory store access (for built-in memory tools).
    erasure_proof : ErasureProofService, optional
        For issuing erasure certificates on delete operations.
    """

    def __init__(
        self,
        db_url: str,
        governance: Any,
        store_manager: Any | None = None,
        erasure_proof: Any | None = None,
        pool=None,
    ) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._pool = pool
        self._governance = governance
        self._store_manager = store_manager
        self._erasure_proof = erasure_proof

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

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Tool Registry schema ensured")

    # ------------------------------------------------------------------
    # Tool CRUD
    # ------------------------------------------------------------------

    def register_tool(
        self,
        tenant_id: str,
        name: str,
        description: str,
        tool_type: ToolType | str,
        input_schema: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
        enabled: bool = True,
        requires_governance: bool = True,
    ) -> ToolDefinition:
        """Register a custom tool for a tenant."""
        now = datetime.now(tz=timezone.utc)
        tool_id = uuid.uuid4().hex[:24]

        if isinstance(tool_type, str):
            tool_type = ToolType(tool_type)

        cfg = config or {}

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO tool_definitions "
            "(tool_id, tenant_id, name, description, tool_type, "
            " input_schema, config, enabled, requires_governance, "
            " is_builtin, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s) "
            "ON CONFLICT (tenant_id, name) DO UPDATE SET "
            "  description = EXCLUDED.description, "
            "  tool_type = EXCLUDED.tool_type, "
            "  input_schema = EXCLUDED.input_schema, "
            "  config = EXCLUDED.config, "
            "  enabled = EXCLUDED.enabled, "
            "  requires_governance = EXCLUDED.requires_governance",
            (
                tool_id, tenant_id, name, description,
                tool_type.value, json.dumps(input_schema),
                json.dumps(cfg), enabled, requires_governance, now,
            ),
        )

        logger.info("Tool registered: %s for tenant %s", name, tenant_id)

        return ToolDefinition(
            tool_id=tool_id,
            tenant_id=tenant_id,
            name=name,
            description=description,
            tool_type=tool_type,
            input_schema=input_schema,
            config=cfg,
            enabled=enabled,
            requires_governance=requires_governance,
            is_builtin=False,
            created_at=now,
        )

    def list_tools(self, tenant_id: str) -> list[ToolDefinition]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM tool_definitions "
            "WHERE tenant_id = %s AND enabled = TRUE "
            "ORDER BY is_builtin DESC, name ASC",
            (tenant_id,),
        ).fetchall()
        return [self._row_to_tool(r) for r in rows]

    def get_tool(self, tenant_id: str, name: str) -> ToolDefinition | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM tool_definitions "
            "WHERE tenant_id = %s AND name = %s AND enabled = TRUE",
            (tenant_id, name),
        ).fetchone()
        return self._row_to_tool(row) if row else None

    def delete_tool(self, tenant_id: str, name: str) -> bool:
        conn = self._get_conn()
        result = conn.execute(
            "DELETE FROM tool_definitions "
            "WHERE tenant_id = %s AND name = %s AND is_builtin = FALSE",
            (tenant_id, name),
        )
        return result.rowcount > 0

    def seed_builtin_tools(self, tenant_id: str) -> int:
        """Register built-in tools for a tenant."""
        count = 0
        now = datetime.now(tz=timezone.utc)

        conn = self._get_conn()
        for tool_def in BUILTIN_TOOLS:
            tool_id = uuid.uuid4().hex[:24]
            try:
                conn.execute(
                    "INSERT INTO tool_definitions "
                    "(tool_id, tenant_id, name, description, tool_type, "
                    " input_schema, config, enabled, requires_governance, "
                    " is_builtin, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, TRUE, %s) "
                    "ON CONFLICT (tenant_id, name) DO UPDATE SET "
                    "  input_schema = EXCLUDED.input_schema, "
                    "  description = EXCLUDED.description",
                    (
                        tool_id, tenant_id, tool_def["name"],
                        tool_def["description"],
                        tool_def["tool_type"].value,
                        json.dumps(tool_def["input_schema"]),
                        json.dumps(tool_def["config"]),
                        tool_def["requires_governance"], now,
                    ),
                )
                count += 1
            except Exception as e:
                logger.warning(f"Error seeding tool {tool_def['name']}: {e}")

        logger.info("Seeded %d built-in tools for tenant %s", count, tenant_id)
        return count

    # ------------------------------------------------------------------
    # Tool definitions for LLM (normalized format)
    # ------------------------------------------------------------------

    def get_tool_definitions(
        self,
        tenant_id: str,
        tool_names: list[str],
    ) -> list[dict[str, Any]]:
        """Get normalized tool definitions for LLM consumption.

        Returns the format expected by LLMRegistry: [{name, description, input_schema}]
        """
        tools = self.list_tools(tenant_id)
        name_set = set(tool_names)
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
            if t.name in name_set
        ]

    # ------------------------------------------------------------------
    # Tool execution — THE CORE METHOD
    # ------------------------------------------------------------------

    def execute(
        self,
        tenant_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        """Execute a tool call with governance.

        1. Look up the tool definition
        2. Validate arguments against JSON Schema
        3. If requires_governance: evaluate_and_gate()
        4. Dispatch to the appropriate executor
        5. Return structured result
        """
        t0 = time.monotonic()

        tool = self.get_tool(tenant_id, tool_name)
        if tool is None:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output=None,
                error=f"Tool '{tool_name}' not found",
            )

        # ── Validate arguments ──
        try:
            self._validate_args(tool.input_schema, arguments)
        except ValueError as e:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output=None,
                error=f"Invalid arguments: {e}",
            )

        # ── Governance gate ──
        gov_logs: list[dict] = []
        if tool.requires_governance and self._governance:
            context = {
                "tool_name": tool_name,
                "store_id": arguments.get("store_id"),
                "query": arguments.get("query", arguments.get("content", "")),
            }
            allowed, logs = self._governance.evaluate_and_gate(
                tenant_id, f"tool:{tool_name}", context,
            )
            gov_logs = [self._governance.log_to_dict(l) for l in logs]

            if not allowed:
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    output=None,
                    error="Governance denied this tool call",
                    governance_allowed=False,
                    governance_logs=gov_logs,
                )

        # ── Execute ──
        try:
            if tool.tool_type == ToolType.MEMORY_READ:
                output = self._exec_memory_read(tool, arguments)
            elif tool.tool_type == ToolType.MEMORY_WRITE:
                output = self._exec_memory_write(tenant_id, tool, arguments)
            elif tool.tool_type == ToolType.MEMORY_DELETE:
                output = self._exec_memory_delete(tenant_id, tool, arguments)
            elif tool.tool_type == ToolType.HTTP_REQUEST:
                output = self._exec_http(tool, arguments)
            elif tool.tool_type == ToolType.CUSTOM:
                output = self._exec_custom(tool, arguments)
            else:
                output = f"Unsupported tool type: {tool.tool_type.value}"

            latency = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                tool_name=tool_name,
                success=True,
                output=output,
                latency_ms=latency,
                governance_allowed=True,
                governance_logs=gov_logs,
            )

        except Exception as e:
            latency = int((time.monotonic() - t0) * 1000)
            logger.error("Tool execution failed: %s — %s", tool_name, e)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output=None,
                error=str(e),
                latency_ms=latency,
                governance_allowed=True,
                governance_logs=gov_logs,
            )

    # ------------------------------------------------------------------
    # Tool executors
    # ------------------------------------------------------------------

    def _exec_memory_read(self, tool: ToolDefinition, args: dict) -> Any:
        """Execute grafomem_retrieve or grafomem_audit."""
        if self._store_manager is None:
            return "Store manager not available"

        store_id = args.get("store_id")
        entry = self._store_manager.get(store_id)
        if entry is None:
            return f"Store '{store_id}' not found"

        if tool.name == "grafomem_audit":
            memories = list(entry.backend.audit())
            return [
                {
                    "ref": m.ref,
                    "content": m.content,
                    "written_at": m.written_at.isoformat() if m.written_at else None,
                    "tenant_id": m.tenant_id,
                }
                for m in memories
            ]

        # grafomem_retrieve
        from aml.backends.interface import RetrieveOptions
        query = args.get("query", "")
        top_k = args.get("top_k", 10)

        results = entry.backend.retrieve(
            query, RetrieveOptions(budget_tokens=1024, top_k=top_k),
        )
        return [
            {
                "ref": m.ref,
                "content": m.content,
                "written_at": m.written_at.isoformat() if m.written_at else None,
            }
            for m in results
        ]

    def _exec_memory_write(self, tenant_id: str, tool: ToolDefinition, args: dict) -> Any:
        """Execute grafomem_write."""
        if self._store_manager is None:
            return "Store manager not available"

        store_id = args.get("store_id")
        content = args.get("content", "")
        entry = self._store_manager.get(store_id)
        if entry is None:
            return f"Store '{store_id}' not found"

        from aml.backends.interface import WriteOptions
        ref = entry.backend.write(
            content, WriteOptions(tenant_id=tenant_id),
        )
        return {"ref": ref, "content": content, "store_id": store_id}

    def _exec_memory_delete(self, tenant_id: str, tool: ToolDefinition, args: dict) -> Any:
        """Execute grafomem_delete (with erasure certificate)."""
        if self._store_manager is None:
            return "Store manager not available"

        store_id = args.get("store_id")
        ref = args.get("ref")
        entry = self._store_manager.get(store_id)
        if entry is None:
            return f"Store '{store_id}' not found"

        deleted = entry.backend.delete(ref)

        # Issue erasure certificate if available
        cert_id = None
        if deleted and self._erasure_proof:
            try:
                cert = self._erasure_proof.issue_certificate(
                    tenant_id=tenant_id,
                    fact_ref=ref,
                )
                cert_id = cert.certificate_id
            except Exception as e:
                logger.warning("Erasure certificate failed: %s", e)

        return {
            "deleted": deleted,
            "ref": ref,
            "store_id": store_id,
            "erasure_certificate_id": cert_id,
        }

    def _exec_http(self, tool: ToolDefinition, args: dict) -> Any:
        """Execute HTTP GET/POST."""
        try:
            import httpx
        except ImportError:
            return "httpx not installed"

        url = args.get("url", "")
        headers = args.get("headers", {})
        method = tool.config.get("method", args.get("method", "GET")).upper()

        with httpx.Client(timeout=30.0) as client:
            if method == "GET":
                resp = client.get(url, headers=headers)
            elif method == "POST":
                body = args.get("body", {})
                resp = client.post(url, json=body, headers=headers)
            else:
                return f"Unsupported HTTP method: {method}"

            # Return truncated response
            text = resp.text[:5000]
            return {
                "status_code": resp.status_code,
                "body": text,
                "truncated": len(resp.text) > 5000,
            }

    def _exec_custom(self, tool: ToolDefinition, args: dict) -> Any:
        """Execute a custom webhook tool."""
        try:
            import httpx
        except ImportError:
            return "httpx not installed"

        url = tool.config.get("webhook_url")
        if not url:
            return "No webhook_url configured for this tool"

        method = tool.config.get("method", "POST").upper()
        headers = tool.config.get("headers", {})

        with httpx.Client(timeout=30.0) as client:
            if method == "GET":
                resp = client.get(url, params=args, headers=headers)
            else:
                resp = client.post(url, json=args, headers=headers)

            try:
                return resp.json()
            except Exception:
                return resp.text[:5000]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_args(schema: dict[str, Any], args: dict[str, Any]) -> None:
        """Validate arguments against JSON Schema."""
        required = schema.get("required", [])
        for field_name in required:
            if field_name not in args:
                raise ValueError(f"Missing required field: '{field_name}'")

        # Type checking for known fields
        properties = schema.get("properties", {})
        for key, value in args.items():
            if key in properties:
                expected_type = properties[key].get("type")
                if expected_type == "string" and not isinstance(value, str):
                    raise ValueError(
                        f"Field '{key}' must be a string, got {type(value).__name__}"
                    )
                elif expected_type == "integer" and not isinstance(value, int):
                    raise ValueError(
                        f"Field '{key}' must be an integer, got {type(value).__name__}"
                    )
                elif expected_type == "object" and not isinstance(value, dict):
                    raise ValueError(
                        f"Field '{key}' must be an object, got {type(value).__name__}"
                    )

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_tool(row: dict[str, Any]) -> ToolDefinition:
        schema = row.get("input_schema")
        if isinstance(schema, str):
            schema = json.loads(schema)
        elif schema is None:
            schema = {}

        cfg = row.get("config")
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        elif cfg is None:
            cfg = {}

        return ToolDefinition(
            tool_id=row["tool_id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            description=row.get("description", ""),
            tool_type=ToolType(row["tool_type"]),
            input_schema=schema,
            config=cfg,
            enabled=row.get("enabled", True),
            requires_governance=row.get("requires_governance", True),
            is_builtin=row.get("is_builtin", False),
            created_at=row["created_at"],
        )

    @staticmethod
    def tool_to_dict(t: ToolDefinition) -> dict[str, Any]:
        return {
            "tool_id": t.tool_id,
            "tenant_id": t.tenant_id,
            "name": t.name,
            "description": t.description,
            "tool_type": t.tool_type.value,
            "input_schema": t.input_schema,
            "config": t.config,
            "enabled": t.enabled,
            "requires_governance": t.requires_governance,
            "is_builtin": t.is_builtin,
            "created_at": t.created_at.isoformat(),
        }
