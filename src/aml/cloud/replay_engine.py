"""
GRAFOMEM Deterministic Replay Engine — re-execute AI decisions with frozen inputs.

Takes a decision_id from the Decision Trail, reconstructs the exact input
context, and re-executes the LLM inference with temperature=0. Compares
the replayed output against the original to determine if the decision
would produce the same result today.

Use cases:
  - Regulator audit: "Prove this decision would be the same today."
  - Model regression testing: replay N decisions against a new model.
  - Incident investigation: pinpoint when model behavior changed.
  - Compliance certification: automated conformance scoring.

Depends on:
  - DecisionTrailService (for fetching original decisions)
  - LLMRegistry (for re-executing inference)
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.replay_engine")


# ============================================================================
# Constants
# ============================================================================

_HASH_BYTES = 32  # BLAKE2b-256


# ============================================================================
# Enumerations
# ============================================================================

class ReplayStatus(str, Enum):
    IDENTICAL = "identical"      # Exact same output
    DIVERGED = "diverged"        # Different output
    IMPOSSIBLE = "impossible"    # Can't replay (model gone, facts deleted)
    DEGRADED = "degraded"        # High similarity but not identical
    ERROR = "error"              # Replay execution failed


# ============================================================================
# Data types
# ============================================================================

@dataclass(slots=True)
class ReplayVerdict:
    """Result of replaying a decision."""
    replay_id: str
    decision_id: str
    tenant_id: str
    status: ReplayStatus
    original_output: str
    replayed_output: str | None
    divergence_reason: str | None
    confidence: float

    # Detailed comparison
    input_reconstructed: bool
    memory_match: bool
    model_available: bool
    output_hash_match: bool

    # Metadata
    model_version_used: str | None
    replay_latency_ms: int | None
    replayed_at: datetime


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS replay_results (
    replay_id           TEXT PRIMARY KEY,
    decision_id         TEXT NOT NULL,
    tenant_id           TEXT NOT NULL,
    status              TEXT NOT NULL,
    original_output     TEXT NOT NULL,
    replayed_output     TEXT,
    divergence_reason   TEXT,
    confidence          REAL NOT NULL DEFAULT 0.0,
    input_reconstructed BOOLEAN DEFAULT FALSE,
    memory_match        BOOLEAN DEFAULT FALSE,
    model_available     BOOLEAN DEFAULT FALSE,
    output_hash_match   BOOLEAN DEFAULT FALSE,
    model_version_used  TEXT,
    replay_latency_ms   INTEGER,
    replayed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rr_decision ON replay_results(decision_id);
CREATE INDEX IF NOT EXISTS idx_rr_tenant ON replay_results(tenant_id, replayed_at DESC);
"""


# ============================================================================
# Hash helpers
# ============================================================================

def _hash_256(data: str) -> str:
    return hashlib.blake2b(data.encode("utf-8"), digest_size=_HASH_BYTES).hexdigest()


# ============================================================================
# ReplayEngine
# ============================================================================

class ReplayEngine:
    """Deterministic replay engine for AI decision verification.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    decision_trail : DecisionTrailService, optional
        For fetching original decisions.
    llm_registry : LLMRegistry, optional
        For re-executing inference.
    store_manager : StoreManager, optional
        For checking if original facts still exist.
    """

    def __init__(
        self,
        db_url: str,
        decision_trail: Any = None,
        llm_registry: Any = None,
        store_manager: Any = None,
        orchestrator: Any = None,
        pool=None,
    ) -> None:
        self._db_url = db_url
        self._pool = pool
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._decision_trail = decision_trail
        self._llm_registry = llm_registry
        self._store_manager = store_manager
        self._orchestrator = orchestrator

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
        logger.info("Replay Engine schema ensured")

    # ------------------------------------------------------------------
    # Replay a single decision
    # ------------------------------------------------------------------

    def replay(self, decision_id: str, tenant_id: str) -> ReplayVerdict:
        """Re-execute a decision with frozen inputs and compare output.

        Steps:
        1. Fetch the original decision record
        2. Reconstruct the input (query + memory context)
        3. Check model availability
        4. Re-execute with temperature=0
        5. Compare output hashes
        6. Persist and return verdict
        """
        replay_id = uuid.uuid4().hex[:24]
        now = datetime.now(tz=timezone.utc)
        start_ts = time.monotonic()

        # 1. Fetch original decision
        if not self._decision_trail:
            return self._make_verdict(
                replay_id, decision_id, tenant_id,
                ReplayStatus.ERROR, "", None,
                "No decision trail service configured",
                now, 0,
            )

        decision = self._decision_trail.get(decision_id)
        if decision is None:
            return self._make_verdict(
                replay_id, decision_id, tenant_id,
                ReplayStatus.IMPOSSIBLE, "", None,
                "Decision not found",
                now, 0,
            )

        if decision.tenant_id != tenant_id:
            return self._make_verdict(
                replay_id, decision_id, tenant_id,
                ReplayStatus.ERROR, "", None,
                "Tenant mismatch",
                now, 0,
            )

        original_output = decision.raw_output
        original_hash = _hash_256(original_output)

        # 2. Reconstruct input
        query = decision.query
        retrieved_contents = decision.retrieved_contents or []
        model_id = decision.model_id
        input_reconstructed = bool(query)

        # 3. Check memory state
        memory_match = True  # Assume true; detailed check if store_manager available
        # TODO: Could check if retrieved refs still exist via store_manager

        # 4. Check model availability
        model_available = False
        model_version_used = getattr(decision, "model_version", None) or model_id

        if not self._llm_registry:
            latency = int((time.monotonic() - start_ts) * 1000)
            return self._make_verdict(
                replay_id, decision_id, tenant_id,
                ReplayStatus.IMPOSSIBLE, original_output, None,
                "No LLM registry configured — cannot re-execute",
                now, latency,
                input_reconstructed=input_reconstructed,
                memory_match=memory_match,
            )

        # Check if the model provider exists
        try:
            providers = self._llm_registry.list_providers(tenant_id)
            provider_model_ids = [p.model_id for p in providers] if providers else []
            model_available = model_id in provider_model_ids
        except Exception:
            model_available = False

        if not model_available:
            latency = int((time.monotonic() - start_ts) * 1000)
            return self._make_verdict(
                replay_id, decision_id, tenant_id,
                ReplayStatus.IMPOSSIBLE, original_output, None,
                f"Model '{model_id}' not available in tenant's LLM registry",
                now, latency,
                input_reconstructed=input_reconstructed,
                memory_match=memory_match,
                model_available=False,
            )

        # 5. Reconstruct original system_prompt and retrieved_facts from agent definition
        original_system_prompt = None
        orchestrator_facts = None  # Full facts with store_id for message reconstruction
        try:
            conn = self._get_conn()
            # Find the orchestrator step that produced this decision
            row = conn.execute(
                "SELECT agent_id, retrieved_facts FROM orchestrator_steps WHERE decision_id = %s LIMIT 1",
                (decision_id,),
            ).fetchone()
            if row:
                agent_id = row["agent_id"]
                # Get the full retrieved_facts (includes store_id) for message reconstruction
                if row.get("retrieved_facts"):
                    import json
                    raw = row["retrieved_facts"]
                    orchestrator_facts = raw if isinstance(raw, list) else json.loads(raw)
                agent_row = conn.execute(
                    "SELECT system_prompt FROM orchestrator_agents WHERE agent_id = %s",
                    (agent_id,),
                ).fetchone()
                if agent_row:
                    original_system_prompt = agent_row["system_prompt"]
                    logger.info(
                        "Replay: reconstructed original system_prompt from agent %s",
                        agent_id,
                    )
        except Exception as e:
            logger.warning("Replay: could not reconstruct system_prompt: %s", e)

        # Fall back to generic prompt only if reconstruction failed
        system_prompt = original_system_prompt or (
            "You are replaying a previous decision. Answer identically."
        )
        if original_system_prompt is None:
            logger.warning(
                "Replay: using fallback system_prompt (agent lookup failed)"
            )

        # 6. Re-execute with temperature=0
        try:
            # Build messages matching the orchestrator's _build_messages format exactly
            messages = []
            if orchestrator_facts:
                # Use the full facts (with store_id) for exact format match
                fact_text = "\n".join(
                    f"- [{f.get('store_id', '?')}] {f.get('content', '')}"
                    for f in orchestrator_facts
                )
                messages.append({
                    "role": "user",
                    "content": (
                        f"[MEMORY CONTEXT — {len(orchestrator_facts)} facts retrieved "
                        f"from GMP stores]\n{fact_text}\n\n"
                        f"[USER QUERY]\n{query}"
                    ),
                })
            elif retrieved_contents:
                # Fallback: use flat contents without store_id
                fact_text = "\n".join(
                    f"- {c}" for c in retrieved_contents
                )
                messages.append({
                    "role": "user",
                    "content": (
                        f"[MEMORY CONTEXT — {len(retrieved_contents)} facts retrieved "
                        f"from GMP stores]\n{fact_text}\n\n"
                        f"[USER QUERY]\n{query}"
                    ),
                })
            else:
                messages.append({"role": "user", "content": query})

            from aml.cloud.llm_registry import LLMRequest
            request = LLMRequest(
                model_id=model_id,
                system_prompt=system_prompt,
                messages=messages,
                tools=None,
                temperature=0.0,
                max_tokens=getattr(decision, "output_tokens", 4096) or 4096,
            )

            response = self._llm_registry.infer(tenant_id, request)
            replayed_output = response.content
            model_version_used = response.model_id

        except Exception as e:
            latency = int((time.monotonic() - start_ts) * 1000)
            logger.error("Replay execution failed for %s: %s", decision_id, e)
            return self._make_verdict(
                replay_id, decision_id, tenant_id,
                ReplayStatus.ERROR, original_output, None,
                f"LLM execution failed: {e}",
                now, latency,
                input_reconstructed=input_reconstructed,
                memory_match=memory_match,
                model_available=True,
            )

        latency = int((time.monotonic() - start_ts) * 1000)

        # 6. Compare outputs
        replayed_hash = _hash_256(replayed_output)
        output_hash_match = original_hash == replayed_hash

        if output_hash_match:
            status = ReplayStatus.IDENTICAL
            confidence = 1.0
            divergence_reason = None
        else:
            # Calculate similarity
            similarity = difflib.SequenceMatcher(
                None, original_output, replayed_output,
            ).ratio()

            if similarity >= 0.9:
                status = ReplayStatus.DEGRADED
                confidence = similarity
                divergence_reason = (
                    f"Output similar ({similarity:.1%}) but not identical"
                )
            else:
                status = ReplayStatus.DIVERGED
                confidence = similarity
                divergence_reason = (
                    f"Output diverged (similarity: {similarity:.1%})"
                )

        verdict = ReplayVerdict(
            replay_id=replay_id,
            decision_id=decision_id,
            tenant_id=tenant_id,
            status=status,
            original_output=original_output,
            replayed_output=replayed_output,
            divergence_reason=divergence_reason,
            confidence=confidence,
            input_reconstructed=input_reconstructed,
            memory_match=memory_match,
            model_available=True,
            output_hash_match=output_hash_match,
            model_version_used=model_version_used,
            replay_latency_ms=latency,
            replayed_at=now,
        )

        self._persist_verdict(verdict)
        logger.info(
            "Replay %s: decision=%s status=%s confidence=%.2f latency=%dms",
            replay_id[:12], decision_id[:12], status.value,
            confidence, latency,
        )

        return verdict

    # ------------------------------------------------------------------
    # Batch replay
    # ------------------------------------------------------------------

    def batch_replay(
        self,
        tenant_id: str,
        sample_size: int = 10,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> list[ReplayVerdict]:
        """Replay a sample of recent decisions for regression testing.

        WARNING: Each replay calls the LLM provider, incurring costs.
        """
        logger.warning(
            "Batch replay requested for tenant %s (sample_size=%d). "
            "This will make %d LLM API calls.",
            tenant_id[:12], sample_size, sample_size,
        )

        if not self._decision_trail:
            return []

        decisions = self._decision_trail.query_decisions(
            tenant_id=tenant_id,
            limit=sample_size,
        )

        verdicts = []
        for decision in decisions:
            verdict = self.replay(decision.decision_id, tenant_id)
            verdicts.append(verdict)

        return verdicts

    # ------------------------------------------------------------------
    # Query replays
    # ------------------------------------------------------------------

    def get_replay(self, replay_id: str) -> ReplayVerdict | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM replay_results WHERE replay_id = %s",
            (replay_id,),
        ).fetchone()
        return self._row_to_verdict(row) if row else None

    def get_replays_for_decision(self, decision_id: str) -> list[ReplayVerdict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM replay_results WHERE decision_id = %s "
            "ORDER BY replayed_at DESC",
            (decision_id,),
        ).fetchall()
        return [self._row_to_verdict(r) for r in rows]

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "  COUNT(CASE WHEN status = 'identical' THEN 1 END) AS identical, "
            "  COUNT(CASE WHEN status = 'diverged' THEN 1 END) AS diverged, "
            "  COUNT(CASE WHEN status = 'impossible' THEN 1 END) AS impossible, "
            "  COUNT(CASE WHEN status = 'degraded' THEN 1 END) AS degraded, "
            "  COUNT(CASE WHEN status = 'error' THEN 1 END) AS errors "
            "FROM replay_results WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        return {
            "total_replays": row["total"] if row else 0,
            "identical": row["identical"] if row else 0,
            "diverged": row["diverged"] if row else 0,
            "impossible": row["impossible"] if row else 0,
            "degraded": row["degraded"] if row else 0,
            "errors": row["errors"] if row else 0,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_verdict(
        self,
        replay_id: str,
        decision_id: str,
        tenant_id: str,
        status: ReplayStatus,
        original_output: str,
        replayed_output: str | None,
        divergence_reason: str | None,
        replayed_at: datetime,
        latency: int,
        input_reconstructed: bool = False,
        memory_match: bool = False,
        model_available: bool = False,
        output_hash_match: bool = False,
    ) -> ReplayVerdict:
        """Create, persist, and return a verdict."""
        confidence = 1.0 if status == ReplayStatus.IDENTICAL else 0.0

        verdict = ReplayVerdict(
            replay_id=replay_id,
            decision_id=decision_id,
            tenant_id=tenant_id,
            status=status,
            original_output=original_output,
            replayed_output=replayed_output,
            divergence_reason=divergence_reason,
            confidence=confidence,
            input_reconstructed=input_reconstructed,
            memory_match=memory_match,
            model_available=model_available,
            output_hash_match=output_hash_match,
            model_version_used=None,
            replay_latency_ms=latency,
            replayed_at=replayed_at,
        )
        self._persist_verdict(verdict)
        return verdict

    def _persist_verdict(self, v: ReplayVerdict) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO replay_results "
            "(replay_id, decision_id, tenant_id, status, "
            " original_output, replayed_output, divergence_reason, "
            " confidence, input_reconstructed, memory_match, "
            " model_available, output_hash_match, "
            " model_version_used, replay_latency_ms, replayed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                v.replay_id, v.decision_id, v.tenant_id,
                v.status.value, v.original_output, v.replayed_output,
                v.divergence_reason, v.confidence,
                v.input_reconstructed, v.memory_match,
                v.model_available, v.output_hash_match,
                v.model_version_used, v.replay_latency_ms,
                v.replayed_at,
            ),
        )

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_verdict(row: dict[str, Any]) -> ReplayVerdict:
        return ReplayVerdict(
            replay_id=row["replay_id"],
            decision_id=row["decision_id"],
            tenant_id=row["tenant_id"],
            status=ReplayStatus(row["status"]),
            original_output=row["original_output"],
            replayed_output=row.get("replayed_output"),
            divergence_reason=row.get("divergence_reason"),
            confidence=row.get("confidence", 0.0),
            input_reconstructed=row.get("input_reconstructed", False),
            memory_match=row.get("memory_match", False),
            model_available=row.get("model_available", False),
            output_hash_match=row.get("output_hash_match", False),
            model_version_used=row.get("model_version_used"),
            replay_latency_ms=row.get("replay_latency_ms"),
            replayed_at=row["replayed_at"],
        )

    @staticmethod
    def verdict_to_dict(v: ReplayVerdict) -> dict[str, Any]:
        return {
            "replay_id": v.replay_id,
            "decision_id": v.decision_id,
            "tenant_id": v.tenant_id,
            "status": v.status.value,
            "original_output": v.original_output[:200] + "..." if len(v.original_output) > 200 else v.original_output,
            "replayed_output": (v.replayed_output[:200] + "...") if v.replayed_output and len(v.replayed_output) > 200 else v.replayed_output,
            "divergence_reason": v.divergence_reason,
            "confidence": v.confidence,
            "input_reconstructed": v.input_reconstructed,
            "memory_match": v.memory_match,
            "model_available": v.model_available,
            "output_hash_match": v.output_hash_match,
            "model_version_used": v.model_version_used,
            "replay_latency_ms": v.replay_latency_ms,
            "replayed_at": v.replayed_at.isoformat(),
        }
