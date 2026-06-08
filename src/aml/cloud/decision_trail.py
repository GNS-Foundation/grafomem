"""
GRAFOMEM Decision Trail — inference audit logging for GRAFOMEM Cloud.

Records every AI inference decision with full provenance: the query posed,
the facts retrieved from memory (with their refs and contents), the model
used, the output produced, and an Ed25519 signature over the decision.

This is the core building block for EU AI Act Article 12 compliance: every
AI decision is logged, signed, and bi-temporally replayable — you can
reconstruct exactly what the system knew when it made any decision.

Backed by PostgreSQL via psycopg v3 (sync), following the same patterns
as ComplianceTracker and MeteringService.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.decision_trail")

# BLAKE2b-128 for decision IDs — matches fact_id in provenance.py
_DECISION_ID_BYTES = 16
_SEP = b"\x1f"  # ASCII unit separator


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class DecisionRecord:
    """A single logged inference decision."""
    decision_id: str
    tenant_id: str
    store_id: str
    session_id: str | None
    created_at: datetime

    # Input context
    query: str
    retrieved_refs: list[int]
    retrieved_contents: list[str]
    retrieval_scores: list[float]
    retrieval_options: dict[str, Any]

    # Model
    model_id: str
    prompt_hash: str | None
    parameters: dict[str, Any]

    # Output
    raw_output: str
    parsed_output: dict[str, Any] | None
    output_tokens: int | None
    latency_ms: int | None

    # Provenance
    signature: bytes | None = None
    public_key: bytes | None = None

    # Lineage
    parent_decision_id: str | None = None


# ============================================================================
# Decision ID computation
# ============================================================================

def compute_decision_id(
    tenant_id: str,
    query: str,
    model_id: str,
    raw_output: str,
    created_at: datetime,
) -> str:
    """BLAKE2b-128 hex digest over canonical decision fields.

    Deterministic: the same inputs always produce the same ID.
    Content-sensitive: any field change produces a different ID.
    """
    h = hashlib.blake2b(digest_size=_DECISION_ID_BYTES)
    for part in [tenant_id, query, model_id, raw_output, created_at.isoformat()]:
        h.update(part.encode("utf-8"))
        h.update(_SEP)
    return h.hexdigest()


def compute_decision_id_bytes(
    tenant_id: str,
    query: str,
    model_id: str,
    raw_output: str,
    created_at: datetime,
) -> bytes:
    """BLAKE2b-128 raw bytes — for Ed25519 signing."""
    h = hashlib.blake2b(digest_size=_DECISION_ID_BYTES)
    for part in [tenant_id, query, model_id, raw_output, created_at.isoformat()]:
        h.update(part.encode("utf-8"))
        h.update(_SEP)
    return h.digest()


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS decision_records (
    decision_id         TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    store_id            TEXT NOT NULL,
    session_id          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Input context
    query               TEXT NOT NULL,
    retrieved_refs      JSONB NOT NULL DEFAULT '[]',
    retrieved_contents  JSONB NOT NULL DEFAULT '[]',
    retrieval_scores    JSONB NOT NULL DEFAULT '[]',
    retrieval_options   JSONB NOT NULL DEFAULT '{}',

    -- Model
    model_id            TEXT NOT NULL,
    prompt_hash         TEXT,
    parameters          JSONB NOT NULL DEFAULT '{}',

    -- Output
    raw_output          TEXT NOT NULL,
    parsed_output       JSONB,
    output_tokens       INTEGER,
    latency_ms          INTEGER,

    -- Provenance
    signature           BYTEA,
    public_key          BYTEA,

    -- Lineage
    parent_decision_id  TEXT REFERENCES decision_records(decision_id)
);
CREATE INDEX IF NOT EXISTS idx_dr_tenant_time
    ON decision_records(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dr_session
    ON decision_records(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dr_store
    ON decision_records(store_id, created_at DESC);
"""


# ============================================================================
# DecisionTrailService
# ============================================================================

class DecisionTrailService:
    """Logs, queries, and replays AI inference decisions.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    """

    def __init__(self, db_url: str, pool=None) -> None:
        self._db_url = db_url
        self._pool = pool
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Return an open connection, creating one lazily."""
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the ``decision_records`` table if it does not exist."""
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        # Migrate existing tables that don't have parent_decision_id
        try:
            conn.execute("ALTER TABLE decision_records ADD COLUMN IF NOT EXISTS parent_decision_id TEXT REFERENCES decision_records(decision_id);")
        except Exception as e:
            logger.warning(f"Could not alter decision_records table: {e}")
        logger.info("Decision Trail schema ensured")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log(
        self,
        tenant_id: str,
        store_id: str,
        query: str,
        model_id: str,
        raw_output: str,
        *,
        session_id: str | None = None,
        retrieved_refs: list[int] | None = None,
        retrieved_contents: list[str] | None = None,
        retrieval_scores: list[float] | None = None,
        retrieval_options: dict[str, Any] | None = None,
        prompt_hash: str | None = None,
        parameters: dict[str, Any] | None = None,
        parsed_output: dict[str, Any] | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        signing_identity=None,
        parent_decision_id: str | None = None,
    ) -> DecisionRecord:
        """Log an inference decision.

        Computes the decision_id, optionally Ed25519-signs it, and
        persists the full record to PostgreSQL.

        Returns the persisted DecisionRecord.
        """
        now = datetime.now(tz=timezone.utc)
        decision_id = compute_decision_id(tenant_id, query, model_id, raw_output, now)

        # Ed25519 signing (optional)
        signature = None
        public_key = None
        if signing_key is not None:
            from aml.provenance import sign_provenance
            did_bytes = compute_decision_id_bytes(
                tenant_id, query, model_id, raw_output, now,
            )
            signature, public_key = sign_provenance(signing_key, did_bytes)

        refs = retrieved_refs or []
        contents = retrieved_contents or []
        scores = retrieval_scores or []
        ret_opts = retrieval_options or {}
        params = parameters or {}

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO decision_records "
            "(decision_id, tenant_id, store_id, session_id, created_at, "
            " query, retrieved_refs, retrieved_contents, retrieval_scores, "
            " retrieval_options, model_id, prompt_hash, parameters, "
            " raw_output, parsed_output, output_tokens, latency_ms, "
            " signature, public_key, parent_decision_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                decision_id, tenant_id, store_id, session_id, now,
                query,
                json.dumps(refs), json.dumps(contents), json.dumps(scores),
                json.dumps(ret_opts),
                model_id, prompt_hash, json.dumps(params),
                raw_output, json.dumps(parsed_output) if parsed_output else None,
                output_tokens, latency_ms,
                signature, public_key,
                parent_decision_id,
            ),
        )
        logger.info(
            "Decision logged: id=%s tenant=%s model=%s",
            decision_id, tenant_id, model_id,
        )

        try:
            from aml.cloud.metrics import DECISIONS_LOGGED
            DECISIONS_LOGGED.labels(model_id=model_id).inc()
        except Exception:
            pass

        return DecisionRecord(
            decision_id=decision_id,
            tenant_id=tenant_id,
            store_id=store_id,
            session_id=session_id,
            created_at=now,
            query=query,
            retrieved_refs=refs,
            retrieved_contents=contents,
            retrieval_scores=scores,
            retrieval_options=ret_opts,
            model_id=model_id,
            prompt_hash=prompt_hash,
            parameters=params,
            raw_output=raw_output,
            parsed_output=parsed_output,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            signature=signature,
            public_key=public_key,
            parent_decision_id=parent_decision_id,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, decision_id: str) -> DecisionRecord | None:
        """Retrieve a single decision by ID."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM decision_records WHERE decision_id = %s",
            (decision_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def query_decisions(
        self,
        tenant_id: str,
        *,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        store_id: str | None = None,
        session_id: str | None = None,
        model_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DecisionRecord]:
        """Query decisions with filters and pagination."""
        clauses = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]

        if from_time is not None:
            clauses.append("created_at >= %s")
            params.append(from_time)
        if to_time is not None:
            clauses.append("created_at <= %s")
            params.append(to_time)
        if store_id is not None:
            clauses.append("store_id = %s")
            params.append(store_id)
        if session_id is not None:
            clauses.append("session_id = %s")
            params.append(session_id)
        if model_id is not None:
            clauses.append("model_id = %s")
            params.append(model_id)

        where = " AND ".join(clauses)
        params.extend([limit, offset])

        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT * FROM decision_records "
            f"WHERE {where} "
            f"ORDER BY created_at DESC "
            f"LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        """Summary stats for a tenant's decision trail."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  MIN(created_at) AS first_decision, "
            "  MAX(created_at) AS last_decision, "
            "  COUNT(DISTINCT model_id) AS models_used, "
            "  COUNT(DISTINCT store_id) AS stores_used, "
            "  COUNT(DISTINCT session_id) AS sessions, "
            "  COALESCE(AVG(latency_ms), 0) AS avg_latency_ms, "
            "  COALESCE(SUM(output_tokens), 0) AS total_tokens "
            "FROM decision_records "
            "WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        if row is None or row["total"] == 0:
            return {"total": 0}

        return {
            "total": row["total"],
            "first_decision": row["first_decision"].isoformat() if row["first_decision"] else None,
            "last_decision": row["last_decision"].isoformat() if row["last_decision"] else None,
            "models_used": row["models_used"],
            "stores_used": row["stores_used"],
            "sessions": row["sessions"],
            "avg_latency_ms": round(row["avg_latency_ms"], 1),
            "total_tokens": row["total_tokens"],
        }

    def export(
        self,
        tenant_id: str,
        *,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream decision records as JSON-safe dicts for bulk export."""
        clauses = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]

        if from_time is not None:
            clauses.append("created_at >= %s")
            params.append(from_time)
        if to_time is not None:
            clauses.append("created_at <= %s")
            params.append(to_time)

        where = " AND ".join(clauses)

        conn = self._get_conn()
        cursor = conn.execute(
            f"SELECT * FROM decision_records "
            f"WHERE {where} "
            f"ORDER BY created_at",
            params,
        )
        for row in cursor:
            rec = self._row_to_record(row)
            yield self._record_to_dict(rec)

    # ------------------------------------------------------------------
    # GDPR — scrub deleted facts from decision records
    # ------------------------------------------------------------------

    def scrub_fact(self, fact_ref: int, tenant_id: str) -> int:
        """Scrub a deleted fact from all decision records.

        Finds all decisions that referenced this fact_ref, replaces the
        corresponding content in retrieved_contents with '[REDACTED]',
        and returns the count of affected decisions.

        This supports GDPR Article 17 (right to erasure) — when a fact
        is hard-deleted from memory, its content is also scrubbed from
        the decision audit trail.
        """
        conn = self._get_conn()

        # Find decisions referencing this fact
        rows = conn.execute(
            "SELECT decision_id, retrieved_refs, retrieved_contents "
            "FROM decision_records "
            "WHERE tenant_id = %s "
            "  AND retrieved_refs @> %s::jsonb",
            (tenant_id, json.dumps([fact_ref])),
        ).fetchall()

        affected = 0
        for row in rows:
            refs = row["retrieved_refs"] if isinstance(row["retrieved_refs"], list) else json.loads(row["retrieved_refs"])
            contents = row["retrieved_contents"] if isinstance(row["retrieved_contents"], list) else json.loads(row["retrieved_contents"])

            # Replace content at matching index positions
            updated = False
            for i, ref in enumerate(refs):
                if ref == fact_ref and i < len(contents):
                    contents[i] = "[REDACTED — GDPR Article 17]"
                    updated = True

            if updated:
                conn.execute(
                    "UPDATE decision_records "
                    "SET retrieved_contents = %s "
                    "WHERE decision_id = %s",
                    (json.dumps(contents), row["decision_id"]),
                )
                affected += 1

        if affected:
            logger.info(
                "GDPR scrub: fact_ref=%s tenant=%s affected=%d decisions",
                fact_ref, tenant_id, affected,
            )
        return affected

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> DecisionRecord:
        """Convert a database row dict into a DecisionRecord."""
        def _load_json(val):
            if val is None:
                return None
            if isinstance(val, (list, dict)):
                return val
            return json.loads(val)

        return DecisionRecord(
            decision_id=row["decision_id"],
            tenant_id=row["tenant_id"],
            store_id=row["store_id"],
            session_id=row.get("session_id"),
            created_at=row["created_at"].astimezone(timezone.utc),
            query=row["query"],
            retrieved_refs=_load_json(row.get("retrieved_refs")) or [],
            retrieved_contents=_load_json(row.get("retrieved_contents")) or [],
            retrieval_scores=_load_json(row.get("retrieval_scores")) or [],
            retrieval_options=_load_json(row.get("retrieval_options")) or {},
            model_id=row["model_id"],
            prompt_hash=row.get("prompt_hash"),
            parameters=_load_json(row.get("parameters")) or {},
            raw_output=row["raw_output"],
            parsed_output=_load_json(row.get("parsed_output")),
            output_tokens=row.get("output_tokens"),
            latency_ms=row.get("latency_ms"),
            signature=row.get("signature"),
            public_key=row.get("public_key"),
            parent_decision_id=row.get("parent_decision_id"),
        )

    @staticmethod
    def _record_to_dict(rec: DecisionRecord) -> dict[str, Any]:
        """Convert a DecisionRecord to a JSON-safe dict (for export/API)."""
        import base64
        return {
            "decision_id": rec.decision_id,
            "tenant_id": rec.tenant_id,
            "store_id": rec.store_id,
            "session_id": rec.session_id,
            "created_at": rec.created_at.isoformat(),
            "query": rec.query,
            "retrieved_refs": rec.retrieved_refs,
            "retrieved_contents": rec.retrieved_contents,
            "retrieval_scores": rec.retrieval_scores,
            "retrieval_options": rec.retrieval_options,
            "model_id": rec.model_id,
            "prompt_hash": rec.prompt_hash,
            "parameters": rec.parameters,
            "raw_output": rec.raw_output,
            "parsed_output": rec.parsed_output,
            "output_tokens": rec.output_tokens,
            "latency_ms": rec.latency_ms,
            "signature": base64.b64encode(rec.signature).decode() if rec.signature else None,
            "public_key": base64.b64encode(rec.public_key).decode() if rec.public_key else None,
            "parent_decision_id": rec.parent_decision_id,
        }
