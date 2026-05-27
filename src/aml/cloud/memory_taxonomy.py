"""
GRAFOMEM Memory Taxonomy — typed memory layers with lifecycle management.

Separates agent memory into five distinct layers with different
lifecycles, governance rules, and trust levels:

  - TENANT_FACTS:      Long-lived per-tenant facts. GDPR-deletable.
  - WORKFLOW_STATE:    Key-value context scoped to a workflow.
  - STEP_CONTEXT:      Ephemeral context scoped to a single step.
  - GLOBAL_KNOWLEDGE:  Shared read-only knowledge across tenants.
  - EXTERNAL_EVIDENCE: Signed, immutable third-party evidence.

Also provides WorkflowContextService for managing workflow/step-scoped
key-value state with automatic cleanup on completion.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.memory_taxonomy")


# ============================================================================
# Enumerations
# ============================================================================

class MemoryLayer(str, Enum):
    """Classification of memory types by lifecycle and governance."""
    TENANT_FACTS = "tenant_facts"
    WORKFLOW_STATE = "workflow_state"
    STEP_CONTEXT = "step_context"
    GLOBAL_KNOWLEDGE = "global_knowledge"
    EXTERNAL_EVIDENCE = "external_evidence"


class ContextLifecycle(str, Enum):
    """How long a memory item lives."""
    PERSISTENT = "persistent"
    WORKFLOW = "workflow"
    STEP = "step"


# ============================================================================
# Data types
# ============================================================================

@dataclass(slots=True)
class TypedMemory:
    """A memory item tagged with its layer and lifecycle metadata."""
    content: str
    layer: MemoryLayer
    ref: int | None = None
    score: float = 0.0
    lifecycle: ContextLifecycle = ContextLifecycle.PERSISTENT
    deletable: bool = True
    trust_level: str = "self"               # "self", "tenant", "external_signed"
    source_signature: bytes | None = None
    source_key: bytes | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkflowContext:
    """A key-value entry scoped to a workflow or step."""
    context_id: str
    workflow_id: str
    tenant_id: str
    key: str
    value: Any
    layer: MemoryLayer
    created_by_step: str | None
    expires_with: ContextLifecycle
    created_at: datetime


# ============================================================================
# Classification helpers
# ============================================================================

_LAYER_PROPERTIES: dict[MemoryLayer, dict[str, Any]] = {
    MemoryLayer.TENANT_FACTS: {
        "lifecycle": ContextLifecycle.PERSISTENT,
        "deletable": True,
        "trust_level": "tenant",
    },
    MemoryLayer.WORKFLOW_STATE: {
        "lifecycle": ContextLifecycle.WORKFLOW,
        "deletable": False,
        "trust_level": "self",
    },
    MemoryLayer.STEP_CONTEXT: {
        "lifecycle": ContextLifecycle.STEP,
        "deletable": False,
        "trust_level": "self",
    },
    MemoryLayer.GLOBAL_KNOWLEDGE: {
        "lifecycle": ContextLifecycle.PERSISTENT,
        "deletable": False,
        "trust_level": "platform",
    },
    MemoryLayer.EXTERNAL_EVIDENCE: {
        "lifecycle": ContextLifecycle.PERSISTENT,
        "deletable": False,
        "trust_level": "external_signed",
    },
}


def classify_memory(
    content: str,
    ref: int | None = None,
    score: float = 0.0,
    source: str = "store",
    source_signature: bytes | None = None,
    source_key: bytes | None = None,
    **kwargs: Any,
) -> TypedMemory:
    """Classify a raw memory item into the appropriate layer.

    Parameters
    ----------
    source : str
        Origin of the memory: "store", "workflow_context", "step_context",
        "global", or "external".
    """
    layer_map = {
        "store": MemoryLayer.TENANT_FACTS,
        "workflow_context": MemoryLayer.WORKFLOW_STATE,
        "step_context": MemoryLayer.STEP_CONTEXT,
        "global": MemoryLayer.GLOBAL_KNOWLEDGE,
        "external": MemoryLayer.EXTERNAL_EVIDENCE,
    }

    if source_signature:
        layer = MemoryLayer.EXTERNAL_EVIDENCE
    else:
        layer = layer_map.get(source, MemoryLayer.TENANT_FACTS)

    props = _LAYER_PROPERTIES[layer]

    return TypedMemory(
        content=content,
        layer=layer,
        ref=ref,
        score=score,
        lifecycle=props["lifecycle"],
        deletable=props["deletable"],
        trust_level=props["trust_level"],
        source_signature=source_signature,
        source_key=source_key,
        metadata=kwargs,
    )


def tag_retrieved_memories(
    memories: list[dict[str, Any]],
    source: str = "store",
) -> list[TypedMemory]:
    """Tag a list of raw retrieved memories with their layer.

    Each dict should have at least "content". Optional: "ref", "score".
    """
    return [
        classify_memory(
            content=m.get("content", str(m)),
            ref=m.get("ref"),
            score=m.get("score", 0.0),
            source=source,
        )
        for m in memories
    ]


def typed_memory_to_dict(tm: TypedMemory) -> dict[str, Any]:
    """Serialize a TypedMemory to JSON-safe dict."""
    return {
        "content": tm.content,
        "layer": tm.layer.value,
        "ref": tm.ref,
        "score": tm.score,
        "lifecycle": tm.lifecycle.value,
        "deletable": tm.deletable,
        "trust_level": tm.trust_level,
        "has_signature": tm.source_signature is not None,
        "metadata": tm.metadata,
    }


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS workflow_context (
    context_id      TEXT PRIMARY KEY,
    workflow_id     TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           JSONB NOT NULL,
    layer           TEXT NOT NULL DEFAULT 'workflow_state',
    created_by_step TEXT,
    expires_with    TEXT NOT NULL DEFAULT 'workflow',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workflow_id, key)
);
CREATE INDEX IF NOT EXISTS idx_wc_workflow ON workflow_context(workflow_id);
CREATE INDEX IF NOT EXISTS idx_wc_step ON workflow_context(created_by_step);
"""


# ============================================================================
# WorkflowContextService
# ============================================================================

class WorkflowContextService:
    """Scoped key-value store for workflow/step-local state.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    """

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Workflow Context Service schema ensured")

    # ------------------------------------------------------------------
    # Set / upsert
    # ------------------------------------------------------------------

    def set(
        self,
        workflow_id: str,
        tenant_id: str,
        key: str,
        value: Any,
        layer: MemoryLayer = MemoryLayer.WORKFLOW_STATE,
        created_by_step: str | None = None,
        expires_with: ContextLifecycle = ContextLifecycle.WORKFLOW,
    ) -> WorkflowContext:
        """Set a key-value pair scoped to a workflow. Upserts on conflict."""
        context_id = uuid.uuid4().hex[:24]
        now = datetime.now(tz=timezone.utc)

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO workflow_context "
            "(context_id, workflow_id, tenant_id, key, value, layer, "
            " created_by_step, expires_with, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (workflow_id, key) DO UPDATE SET "
            "  value = EXCLUDED.value, "
            "  created_by_step = EXCLUDED.created_by_step, "
            "  created_at = EXCLUDED.created_at",
            (
                context_id, workflow_id, tenant_id, key,
                json.dumps(value), layer.value,
                created_by_step, expires_with.value, now,
            ),
        )

        return WorkflowContext(
            context_id=context_id,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            key=key,
            value=value,
            layer=layer,
            created_by_step=created_by_step,
            expires_with=expires_with,
            created_at=now,
        )

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    def get(self, workflow_id: str, key: str) -> Any | None:
        """Get a value by key. Returns None if not found."""
        ctx = self.get_context(workflow_id, key)
        return ctx.value if ctx else None

    def get_context(self, workflow_id: str, key: str) -> WorkflowContext | None:
        """Get the full context object for a key."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM workflow_context "
            "WHERE workflow_id = %s AND key = %s",
            (workflow_id, key),
        ).fetchone()
        return self._row_to_context(row) if row else None

    def list_context(self, workflow_id: str) -> list[WorkflowContext]:
        """List all context entries for a workflow."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM workflow_context WHERE workflow_id = %s "
            "ORDER BY created_at ASC",
            (workflow_id,),
        ).fetchall()
        return [self._row_to_context(r) for r in rows]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_workflow(self, workflow_id: str) -> int:
        """Delete all context for a completed workflow. Returns count deleted."""
        conn = self._get_conn()
        result = conn.execute(
            "DELETE FROM workflow_context WHERE workflow_id = %s",
            (workflow_id,),
        )
        count = result.rowcount
        if count:
            logger.info("Cleaned up %d context entries for workflow %s", count, workflow_id[:12])
        return count

    def cleanup_step(self, step_id: str) -> int:
        """Delete step-scoped context entries. Returns count deleted."""
        conn = self._get_conn()
        result = conn.execute(
            "DELETE FROM workflow_context "
            "WHERE created_by_step = %s AND expires_with = 'step'",
            (step_id,),
        )
        count = result.rowcount
        if count:
            logger.info("Cleaned up %d step-scoped context entries for step %s", count, step_id[:12])
        return count

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_context(row: dict[str, Any]) -> WorkflowContext:
        val = row.get("value")
        if isinstance(val, str):
            val = json.loads(val)

        return WorkflowContext(
            context_id=row["context_id"],
            workflow_id=row["workflow_id"],
            tenant_id=row["tenant_id"],
            key=row["key"],
            value=val,
            layer=MemoryLayer(row.get("layer", "workflow_state")),
            created_by_step=row.get("created_by_step"),
            expires_with=ContextLifecycle(row.get("expires_with", "workflow")),
            created_at=row["created_at"],
        )

    @staticmethod
    def context_to_dict(ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "context_id": ctx.context_id,
            "workflow_id": ctx.workflow_id,
            "tenant_id": ctx.tenant_id,
            "key": ctx.key,
            "value": ctx.value,
            "layer": ctx.layer.value,
            "created_by_step": ctx.created_by_step,
            "expires_with": ctx.expires_with.value,
            "created_at": ctx.created_at.isoformat(),
        }
