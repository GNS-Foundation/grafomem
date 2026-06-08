"""
GRAFOMEM Execution Receipts — hash-chained attestation for orchestrator workflows.

Every orchestrator step produces an ExecutionReceipt containing:
  - BLAKE2b-256 hashes of inputs, outputs, memory, governance verdicts
  - A ``previous_receipt_hash`` linking to the prior step's receipt
  - An Ed25519 signature over the receipt ID

Tampering with any step invalidates all subsequent receipts. This is
AI notarization — cryptographic proof that a workflow executed
these steps in this order.

Chain verification is O(n) over the number of steps.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.execution_receipts")


# ============================================================================
# Constants
# ============================================================================

_RECEIPT_ID_BYTES = 16   # BLAKE2b-128
_HASH_BYTES = 32         # BLAKE2b-256
_SEP = b"\x1f"           # ASCII Unit Separator — same as provenance.py


# ============================================================================
# Enumerations
# ============================================================================

class ChainStatus(str, Enum):
    INTACT = "intact"
    TAMPERED = "tampered"
    EMPTY = "empty"


# ============================================================================
# Data types
# ============================================================================

@dataclass(slots=True)
class ExecutionReceipt:
    """Tamper-evident, hash-chained execution attestation."""
    receipt_id: str                    # BLAKE2b-128 hex
    step_id: str
    workflow_id: str
    tenant_id: str
    step_number: int
    previous_receipt_hash: str | None  # BLAKE2b-256 hex of previous receipt_id

    # Input attestation
    input_hash: str                    # BLAKE2b-256(input_text)
    memory_snapshot_hash: str          # BLAKE2b-256(joined retrieved_contents)
    policy_evaluation_hash: str        # BLAKE2b-256(governance_logs json)

    # Execution attestation
    model_id: str
    model_version: str | None
    tool_call_hashes: list[str]        # BLAKE2b-256 per tool call+result

    # Output attestation
    output_hash: str                   # BLAKE2b-256(raw_output)
    decision_id: str | None            # Links to Decision Trail

    # Timing
    started_at: datetime
    completed_at: datetime

    # Cryptographic seal
    signature: bytes | None = None
    public_key: bytes | None = None


@dataclass(slots=True)
class ChainVerdict:
    """Result of verifying a receipt chain."""
    status: ChainStatus
    steps_verified: int
    tampered_at_step: int | None = None
    reason: str | None = None


# ============================================================================
# Hash helpers
# ============================================================================

def _hash_256(data: str) -> str:
    """BLAKE2b-256 hex digest of a UTF-8 string."""
    return hashlib.blake2b(data.encode("utf-8"), digest_size=_HASH_BYTES).hexdigest()


def _hash_256_bytes(data: bytes) -> str:
    """BLAKE2b-256 hex digest of raw bytes."""
    return hashlib.blake2b(data, digest_size=_HASH_BYTES).hexdigest()


def _compute_receipt_id(
    step_id: str,
    workflow_id: str,
    tenant_id: str,
    step_number: int,
    previous_receipt_hash: str | None,
    input_hash: str,
    memory_snapshot_hash: str,
    policy_evaluation_hash: str,
    model_id: str,
    model_version: str | None,
    tool_call_hashes: list[str],
    output_hash: str,
    decision_id: str | None,
    started_at: datetime,
    completed_at: datetime,
) -> str:
    """BLAKE2b-128 over all receipt fields (excluding signature)."""
    parts = [
        step_id.encode(),
        workflow_id.encode(),
        tenant_id.encode(),
        str(step_number).encode(),
        (previous_receipt_hash or "").encode(),
        input_hash.encode(),
        memory_snapshot_hash.encode(),
        policy_evaluation_hash.encode(),
        model_id.encode(),
        (model_version or "").encode(),
        json.dumps(tool_call_hashes, sort_keys=True).encode(),
        output_hash.encode(),
        (decision_id or "").encode(),
        started_at.isoformat().encode(),
        completed_at.isoformat().encode(),
    ]
    payload = _SEP.join(parts)
    return hashlib.blake2b(payload, digest_size=_RECEIPT_ID_BYTES).hexdigest()


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS execution_receipts (
    receipt_id              TEXT PRIMARY KEY,
    step_id                 TEXT NOT NULL,
    workflow_id             TEXT NOT NULL,
    tenant_id               TEXT NOT NULL,
    step_number             INTEGER NOT NULL,
    previous_receipt_hash   TEXT,

    input_hash              TEXT NOT NULL,
    memory_snapshot_hash    TEXT NOT NULL,
    policy_evaluation_hash  TEXT NOT NULL,

    model_id                TEXT NOT NULL,
    model_version           TEXT,
    tool_call_hashes        JSONB DEFAULT '[]',

    output_hash             TEXT NOT NULL,
    decision_id             TEXT,

    started_at              TIMESTAMPTZ NOT NULL,
    completed_at            TIMESTAMPTZ NOT NULL,

    signature               BYTEA,
    public_key              BYTEA
);
CREATE INDEX IF NOT EXISTS idx_er_workflow
    ON execution_receipts(workflow_id, step_number);
CREATE INDEX IF NOT EXISTS idx_er_tenant
    ON execution_receipts(tenant_id, completed_at DESC);
"""


# ============================================================================
# ExecutionReceiptService
# ============================================================================

class ExecutionReceiptService:
    """Hash-chained execution attestation for orchestrator workflows.

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
        logger.info("Execution Receipt Service schema ensured")

    # ------------------------------------------------------------------
    # Issue receipt
    # ------------------------------------------------------------------

    def issue_receipt(
        self,
        tenant_id: str,
        step_id: str,
        workflow_id: str,
        step_number: int,
        input_text: str,
        retrieved_contents: list[str],
        governance_logs: list[dict],
        model_id: str,
        raw_output: str,
        decision_id: str | None = None,
        model_version: str | None = None,
        tool_calls: list[dict] | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        signing_key: bytes | None = None,
    ) -> ExecutionReceipt:
        """Issue a new receipt and chain it to the previous step.

        Computes BLAKE2b hashes of all inputs and outputs, links to the
        previous receipt, optionally signs with Ed25519, and persists.
        """
        now = datetime.now(tz=timezone.utc)
        started = started_at or now
        completed = completed_at or now

        # Hash inputs
        input_hash = _hash_256(input_text)
        memory_snapshot_hash = _hash_256(
            "\n".join(sorted(retrieved_contents)) if retrieved_contents else ""
        )
        policy_evaluation_hash = _hash_256(
            json.dumps(governance_logs, sort_keys=True, default=str)
        )
        output_hash = _hash_256(raw_output)

        # Hash each tool call
        tool_call_hashes = []
        for tc in (tool_calls or []):
            tc_str = json.dumps(tc, sort_keys=True, default=str)
            tool_call_hashes.append(_hash_256(tc_str))

        # Find previous receipt for chain linkage
        previous_receipt_hash = None
        if step_number > 0:
            prev = self._get_previous_receipt(workflow_id, step_number)
            if prev:
                previous_receipt_hash = _hash_256(prev.receipt_id)

        # Compute receipt ID
        receipt_id = _compute_receipt_id(
            step_id=step_id,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            step_number=step_number,
            previous_receipt_hash=previous_receipt_hash,
            input_hash=input_hash,
            memory_snapshot_hash=memory_snapshot_hash,
            policy_evaluation_hash=policy_evaluation_hash,
            model_id=model_id,
            model_version=model_version,
            tool_call_hashes=tool_call_hashes,
            output_hash=output_hash,
            decision_id=decision_id,
            started_at=started,
            completed_at=completed,
        )

        # Optional Ed25519 signing
        signature = None
        public_key = None
        if signing_key:
            try:
                from aml.provenance import sign_provenance
                receipt_id_bytes = bytes.fromhex(receipt_id)
                signature, public_key = sign_provenance(signing_key, receipt_id_bytes)
            except Exception as e:
                logger.warning("Receipt signing failed: %s", e)

        receipt = ExecutionReceipt(
            receipt_id=receipt_id,
            step_id=step_id,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            step_number=step_number,
            previous_receipt_hash=previous_receipt_hash,
            input_hash=input_hash,
            memory_snapshot_hash=memory_snapshot_hash,
            policy_evaluation_hash=policy_evaluation_hash,
            model_id=model_id,
            model_version=model_version,
            tool_call_hashes=tool_call_hashes,
            output_hash=output_hash,
            decision_id=decision_id,
            started_at=started,
            completed_at=completed,
            signature=signature,
            public_key=public_key,
        )

        # Persist
        self._persist_receipt(receipt)
        logger.info(
            "Receipt issued: %s (workflow=%s step=%d chain=%s)",
            receipt_id[:12], workflow_id[:12], step_number,
            "linked" if previous_receipt_hash else "genesis",
        )

        return receipt

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_receipt(self, receipt_id: str) -> ExecutionReceipt | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM execution_receipts WHERE receipt_id = %s",
            (receipt_id,),
        ).fetchone()
        return self._row_to_receipt(row) if row else None

    def get_receipts(self, workflow_id: str) -> list[ExecutionReceipt]:
        """Get all receipts for a workflow, ordered by step number."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM execution_receipts WHERE workflow_id = %s "
            "ORDER BY step_number ASC",
            (workflow_id,),
        ).fetchall()
        return [self._row_to_receipt(r) for r in rows]

    # ------------------------------------------------------------------
    # Chain verification
    # ------------------------------------------------------------------

    def verify_chain(self, workflow_id: str) -> ChainVerdict:
        """Verify the full hash chain for a workflow.

        O(n) verification: checks receipt ID integrity, Ed25519
        signatures, and chain linkage for every step.
        """
        receipts = self.get_receipts(workflow_id)

        if not receipts:
            return ChainVerdict(
                status=ChainStatus.EMPTY, steps_verified=0,
            )

        for i, receipt in enumerate(receipts):
            # 1. Verify receipt_id integrity
            recomputed = _compute_receipt_id(
                step_id=receipt.step_id,
                workflow_id=receipt.workflow_id,
                tenant_id=receipt.tenant_id,
                step_number=receipt.step_number,
                previous_receipt_hash=receipt.previous_receipt_hash,
                input_hash=receipt.input_hash,
                memory_snapshot_hash=receipt.memory_snapshot_hash,
                policy_evaluation_hash=receipt.policy_evaluation_hash,
                model_id=receipt.model_id,
                model_version=receipt.model_version,
                tool_call_hashes=receipt.tool_call_hashes,
                output_hash=receipt.output_hash,
                decision_id=receipt.decision_id,
                started_at=receipt.started_at,
                completed_at=receipt.completed_at,
            )
            if receipt.receipt_id != recomputed:
                return ChainVerdict(
                    status=ChainStatus.TAMPERED,
                    steps_verified=i,
                    tampered_at_step=i,
                    reason="receipt_id_mismatch",
                )

            # 2. Verify Ed25519 signature if present
            if receipt.signature and receipt.public_key:
                try:
                    from aml.provenance import verify_provenance
                    receipt_id_bytes = bytes.fromhex(receipt.receipt_id)
                    valid = verify_provenance(
                        receipt.public_key, receipt_id_bytes, receipt.signature,
                    )
                    if not valid:
                        return ChainVerdict(
                            status=ChainStatus.TAMPERED,
                            steps_verified=i,
                            tampered_at_step=i,
                            reason="signature_invalid",
                        )
                except Exception as e:
                    return ChainVerdict(
                        status=ChainStatus.TAMPERED,
                        steps_verified=i,
                        tampered_at_step=i,
                        reason=f"signature_verification_error: {e}",
                    )

            # 3. Verify chain linkage
            if i == 0:
                # Genesis receipt should have no parent
                if receipt.previous_receipt_hash is not None:
                    return ChainVerdict(
                        status=ChainStatus.TAMPERED,
                        steps_verified=0,
                        tampered_at_step=0,
                        reason="genesis_has_parent",
                    )
            else:
                # Non-genesis: previous_receipt_hash must match
                expected = _hash_256(receipts[i - 1].receipt_id)
                if receipt.previous_receipt_hash != expected:
                    return ChainVerdict(
                        status=ChainStatus.TAMPERED,
                        steps_verified=i,
                        tampered_at_step=i,
                        reason="chain_broken",
                    )

        return ChainVerdict(
            status=ChainStatus.INTACT,
            steps_verified=len(receipts),
        )

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "  COUNT(DISTINCT workflow_id) AS workflows "
            "FROM execution_receipts WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
        return {
            "total_receipts": row["total"] if row else 0,
            "workflows_with_receipts": row["workflows"] if row else 0,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_previous_receipt(
        self, workflow_id: str, step_number: int,
    ) -> ExecutionReceipt | None:
        """Find the receipt for the step immediately before step_number."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM execution_receipts "
            "WHERE workflow_id = %s AND step_number < %s "
            "ORDER BY step_number DESC LIMIT 1",
            (workflow_id, step_number),
        ).fetchone()
        return self._row_to_receipt(row) if row else None

    def _persist_receipt(self, r: ExecutionReceipt) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO execution_receipts "
            "(receipt_id, step_id, workflow_id, tenant_id, step_number, "
            " previous_receipt_hash, input_hash, memory_snapshot_hash, "
            " policy_evaluation_hash, model_id, model_version, "
            " tool_call_hashes, output_hash, decision_id, "
            " started_at, completed_at, signature, public_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                r.receipt_id, r.step_id, r.workflow_id, r.tenant_id,
                r.step_number, r.previous_receipt_hash,
                r.input_hash, r.memory_snapshot_hash,
                r.policy_evaluation_hash,
                r.model_id, r.model_version,
                json.dumps(r.tool_call_hashes),
                r.output_hash, r.decision_id,
                r.started_at, r.completed_at,
                r.signature, r.public_key,
            ),
        )

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_receipt(row: dict[str, Any]) -> ExecutionReceipt:
        tch = row.get("tool_call_hashes")
        if isinstance(tch, str):
            tch = json.loads(tch)
        elif tch is None:
            tch = []

        return ExecutionReceipt(
            receipt_id=row["receipt_id"],
            step_id=row["step_id"],
            workflow_id=row["workflow_id"],
            tenant_id=row["tenant_id"],
            step_number=row["step_number"],
            previous_receipt_hash=row.get("previous_receipt_hash"),
            input_hash=row["input_hash"],
            memory_snapshot_hash=row["memory_snapshot_hash"],
            policy_evaluation_hash=row["policy_evaluation_hash"],
            model_id=row["model_id"],
            model_version=row.get("model_version"),
            tool_call_hashes=tch,
            output_hash=row["output_hash"],
            decision_id=row.get("decision_id"),
            started_at=row["started_at"].astimezone(timezone.utc),
            completed_at=row["completed_at"].astimezone(timezone.utc),
            signature=row.get("signature"),
            public_key=row.get("public_key"),
        )

    @staticmethod
    def receipt_to_dict(r: ExecutionReceipt) -> dict[str, Any]:
        return {
            "receipt_id": r.receipt_id,
            "step_id": r.step_id,
            "workflow_id": r.workflow_id,
            "tenant_id": r.tenant_id,
            "step_number": r.step_number,
            "previous_receipt_hash": r.previous_receipt_hash,
            "input_hash": r.input_hash,
            "memory_snapshot_hash": r.memory_snapshot_hash,
            "policy_evaluation_hash": r.policy_evaluation_hash,
            "model_id": r.model_id,
            "model_version": r.model_version,
            "tool_call_hashes": r.tool_call_hashes,
            "output_hash": r.output_hash,
            "decision_id": r.decision_id,
            "started_at": r.started_at.isoformat(),
            "completed_at": r.completed_at.isoformat(),
            "signature": base64.b64encode(r.signature).decode() if r.signature else None,
            "public_key": base64.b64encode(r.public_key).decode() if r.public_key else None,
        }

    @staticmethod
    def verdict_to_dict(v: ChainVerdict) -> dict[str, Any]:
        return {
            "status": v.status.value,
            "steps_verified": v.steps_verified,
            "tampered_at_step": v.tampered_at_step,
            "reason": v.reason,
        }
