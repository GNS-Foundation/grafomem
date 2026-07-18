"""
HITL Routes — Human-in-the-loop cryptographic attestation.

GET  /v1/hitl/requests/{request_id}
POST /v1/hitl/requests/{request_id}/attest
GET  /v1/hitl/requests/{request_id}/verify
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aml.server.scopes import require_scope
from aml.cloud.db_pool import DatabasePool
from aml.cloud.orchestrator import OrchestratorService
from aml.cloud.gcrumbs import GcrumbsService

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature


def _unsafe_dev_enabled() -> bool:
    """Whether the local-dev approver auto-register bypass may run.

    The UNSAFE_LOCAL_DEV path auto-registers ANY signer as an approver, i.e. it
    lets any key approve any request. It must NEVER be reachable in a deployed
    environment. We require the explicit opt-in flag AND the absence of any
    production/deployment markers as belt-and-suspenders.
    """
    if os.environ.get("UNSAFE_LOCAL_DEV") != "true":
        return False
    env = os.environ.get("ENVIRONMENT", os.environ.get("APP_ENV", "")).lower()
    if env in ("production", "prod", "staging"):
        return False
    for marker in ("RAILWAY_ENVIRONMENT", "RAILWAY_PUBLIC_DOMAIN",
                   "KUBERNETES_SERVICE_HOST", "DYNO"):
        if os.environ.get(marker):
            return False
    return True


class AttestRequest(BaseModel):
    decision: str  # "approve" or "deny"
    signer_id: str
    signature: str

def create_hitl_router(db_pool: DatabasePool, orchestrator: OrchestratorService, gcrumbs: GcrumbsService) -> APIRouter:
    router = APIRouter(prefix="/v1/hitl", tags=["hitl"])

    @router.get("/requests")
    def list_requests(request: Request, status: str = "pending"):
        tenant_id = require_scope(request, "compliance:read")
        
        with db_pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT request_id, workflow_id, step_id, action, resource, issued_at, expires_at, status 
                FROM hitl_approval_requests 
                WHERE tenant_id = %s AND status = %s
                ORDER BY issued_at DESC
                """,
                (tenant_id, status)
            ).fetchall()
            
        return {
            "requests": [
                {
                    "request_id": row["request_id"],
                    "workflow_id": row["workflow_id"],
                    "step_id": row["step_id"],
                    "action": row["action"],
                    "resource": row["resource"],
                    "issued_at": row["issued_at"].isoformat() if row["issued_at"] else None,
                    "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
                    "status": row["status"]
                }
                for row in rows
            ]
        }

    @router.get("/requests/{request_id}")
    def get_request(request_id: str, request: Request):
        signature = request.headers.get("X-GNS-Signature")
        timestamp_str = request.headers.get("X-GNS-Timestamp")
        signer_id = request.headers.get("X-GNS-Signer")

        if not signature or not timestamp_str or not signer_id:
            raise HTTPException(401, "Missing signature, timestamp, or signer headers")

        try:
            timestamp = int(timestamp_str)
        except ValueError:
            raise HTTPException(400, "Invalid timestamp")

        now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        if abs(now_ts - timestamp) > 60000:
            raise HTTPException(401, "Stale or future timestamp")

        challenge_str = f"grafomem.hitl.fetch.v1:{request_id}:{timestamp_str}"
        challenge_bytes = challenge_str.encode("utf-8")

        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(signer_id))
            pub.verify(bytes.fromhex(signature), challenge_bytes)
        except Exception:
            raise HTTPException(401, "Invalid signature")

        with db_pool.connection() as conn:
            row = conn.execute(
                "SELECT context_json, context_bytes, expires_at, status, tenant_id FROM hitl_approval_requests WHERE request_id = %s",
                (request_id,)
            ).fetchone()
        
        if not row:
            raise HTTPException(404, "Request not found")
        
        if row["status"] != "pending":
            raise HTTPException(400, f"Request is no longer pending (status: {row['status']})")

        # Verify the signer is an active approver for this tenant
        with db_pool.connection() as conn:
            approver = conn.execute(
                "SELECT 1 FROM hitl_approvers WHERE approver_id = %s AND tenant_id = %s AND active = TRUE",
                (signer_id, row["tenant_id"])
            ).fetchone()
            
            if not approver and not _unsafe_dev_enabled():
                raise HTTPException(403, "Not an active approver for this request")

        return {
            "request_id": request_id,
            "context_json": row["context_json"],
            "context_bytes_hex": row["context_bytes"].hex(),
            "expires_at": row["expires_at"].isoformat()
        }

    @router.post("/requests/{request_id}/attest")
    def attest_request(request_id: str, body: AttestRequest, request: Request):
        if body.decision not in ("approve", "deny"):
            raise HTTPException(400, "Decision must be 'approve' or 'deny'")

        with db_pool.connection() as conn:
            # Atomic fetch and lock
            row = conn.execute(
                "SELECT * FROM hitl_approval_requests WHERE request_id = %s FOR UPDATE",
                (request_id,)
            ).fetchone()

            if not row:
                raise HTTPException(404, "Request not found")
            
            if row["status"] != "pending":
                raise HTTPException(400, "Request already decided or expired")

            if datetime.now(timezone.utc) > row["expires_at"]:
                # Update to expired
                conn.execute(
                    "UPDATE hitl_approval_requests SET status = 'expired' WHERE request_id = %s",
                    (request_id,)
                )
                raise HTTPException(400, "Request has expired")

            # Look up approver
            approver = conn.execute(
                "SELECT public_key FROM hitl_approvers WHERE approver_id = %s AND tenant_id = %s AND active = TRUE",
                (body.signer_id, row["tenant_id"])
            ).fetchone()

            if not approver:
                if _unsafe_dev_enabled():
                    # LOCAL DEV ONLY (never in a deployed env): auto-register the
                    # device so a fresh test key can attest. Gated by
                    # _unsafe_dev_enabled() — see its docstring.
                    conn.execute(
                        "INSERT INTO hitl_approvers (tenant_id, approver_id, public_key, active) VALUES (%s, %s, %s, TRUE) ON CONFLICT DO NOTHING",
                        (row["tenant_id"], body.signer_id, body.signer_id)
                    )
                    pub_key_hex = body.signer_id
                else:
                    raise HTTPException(403, "Signer not authorized")
            else:
                pub_key_hex = approver["public_key"]
            
            # Reconstruct exact signed bytes
            # The client signs: prefix + context_bytes + b"\x1f" + decision
            prefix = b"grafomem.hitl.approval.v1:"
            signed_bytes = prefix + row["context_bytes"] + b"\x1f" + body.decision.encode("utf-8")

            # Verify Ed25519 signature
            try:
                pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_key_hex))
                pub.verify(bytes.fromhex(body.signature), signed_bytes)
            except InvalidSignature:
                raise HTTPException(401, "Invalid signature")
            except Exception as e:
                raise HTTPException(400, f"Verification error: {e}")

            # Update request status
            conn.execute(
                """
                UPDATE hitl_approval_requests 
                SET status = %s, signer_id = %s, signature = %s, decided_at = %s 
                WHERE request_id = %s
                """,
                (body.decision + "d", body.signer_id, body.signature, datetime.now(timezone.utc), request_id)
            )

            # Append gcrumbs breadcrumb in the same transaction
            event_type = f"hitl:{body.decision}d"
            gcrumbs.append_breadcrumb(
                tenant_id=row["tenant_id"],
                event_type=event_type,
                payload={
                    "request_id": request_id,
                    "workflow_id": row["workflow_id"],
                    "step_id": row["step_id"],
                    "signer_id": body.signer_id,
                    "signer_pubkey": pub_key_hex,
                    "decision": body.decision,
                    "context_bytes_hex": row["context_bytes"].hex(),
                    "signature": body.signature
                },
                conn=conn
            )

        # Outside the transaction, resume workflow
        approved = (body.decision == "approve")
        resume_failed = False
        try:
            orchestrator.resume_workflow(row["workflow_id"], approved)
        except Exception as e:
            import logging
            logger = logging.getLogger("grafomem.cloud.hitl")
            logger.error("Failed to resume workflow %s for request %s", row["workflow_id"], request_id, exc_info=True)
            resume_failed = True

        response_data = {"status": body.decision + "d"}
        if resume_failed:
            response_data["warning"] = "workflow_resume_failed"
        return response_data

    @router.get("/approvers/{approver_id}/requests")
    def list_approver_requests(approver_id: str, request: Request):
        signature = request.headers.get("X-GNS-Signature")
        timestamp_str = request.headers.get("X-GNS-Timestamp")

        if not signature or not timestamp_str:
            raise HTTPException(401, "Missing signature or timestamp headers")

        try:
            timestamp = int(timestamp_str)
        except ValueError:
            raise HTTPException(400, "Invalid timestamp")

        now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        if abs(now_ts - timestamp) > 60000:
            raise HTTPException(401, "Stale or future timestamp")

        challenge_str = f"grafomem.hitl.inbox.v1:{approver_id}:{timestamp_str}"
        challenge_bytes = challenge_str.encode("utf-8")

        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(approver_id))
            pub.verify(bytes.fromhex(signature), challenge_bytes)
        except Exception:
            raise HTTPException(401, "Invalid signature")

        with db_pool.connection() as conn:
            approver_rows = conn.execute(
                """
                SELECT tenant_id FROM hitl_approvers
                WHERE approver_id = %s AND active = TRUE
                """,
                (approver_id,),
            ).fetchall()

            # A cryptographically valid signature from a key that is NOT an
            # active approver is authenticated but not authorized. Return 403 so
            # a revoked/unknown approver gets a clear signal instead of a
            # silently-empty inbox (which would read as "nothing pending").
            if not approver_rows:
                raise HTTPException(403, "Not an active approver")

            tenant_ids = [row["tenant_id"] for row in approver_rows]

            rows = conn.execute(
                """
                SELECT request_id, action, resource, expires_at, tenant_id
                FROM hitl_approval_requests
                WHERE status = 'pending'
                  AND expires_at > %s
                  AND tenant_id = ANY(%s)
                ORDER BY issued_at DESC
                LIMIT 50
                """,
                (datetime.now(timezone.utc), tenant_ids),
            ).fetchall()

        return {
            "requests": [
                {
                    "request_id": row["request_id"],
                    "action": row["action"],
                    "resource": row["resource"],
                    "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
                    "tenant_id": row["tenant_id"],
                }
                for row in rows
            ]
        }

    @router.get("/requests/{request_id}/verify")
    def verify_request(request_id: str, request: Request):
        require_scope(request, "compliance:read")
        with db_pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM hitl_approval_requests WHERE request_id = %s",
                (request_id,)
            ).fetchone()
        
        if not row:
            raise HTTPException(404, "Request not found")
        
        return {
            "request_id": request_id,
            "status": row["status"],
            "signer_id": row["signer_id"],
            "signature": row["signature"],
            "context_bytes_hex": row["context_bytes"].hex() if row["context_bytes"] else None
        }

    return router
