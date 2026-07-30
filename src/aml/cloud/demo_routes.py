"""Governed-decision + independent-verification routes.

Adds the three endpoints the Kapwork demo is built on:

  POST /v1/governed/decisions   Record a governed decision -> returns the
                                decision_record AND a signed, hash-chained
                                execution_receipt (tenant-scoped, auth required).
  GET  /v1/gcrumbs/verify/key    The real Ed25519 public key (auth-exempt) so an
                                independent party can fetch it without access.
  POST /v1/gcrumbs/verify        Stateless verification of one or more receipts
                                against a supplied public key. No DB access.

The receipt is signed over its receipt_id by the shared signing identity, so a
funder can verify it themselves and a wrong key is rejected.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from aml.cloud.execution_receipts import ExecutionReceiptService

logger = logging.getLogger("grafomem.cloud.demo_routes")


def _tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


# ----------------------------------------------------------------------------
# POST /v1/governed/decisions
# ----------------------------------------------------------------------------

class GovernedDecisionRequest(BaseModel):
    decision: str                                    # "certify" | "reject"
    reason: str = ""
    invoice_id: str | None = None
    context: dict = Field(default_factory=dict)      # the invoice fields
    model_id: str = "kapwork-verify-agent-v1"


def create_governed_router(decision_trail, execution_receipts, signing_identity) -> APIRouter:
    router = APIRouter(tags=["Governed Decisions"])

    @router.post("/v1/governed/decisions")
    async def governed_decision(req: GovernedDecisionRequest, request: Request):
        tenant_id = _tenant_id(request)
        if execution_receipts is None or decision_trail is None:
            raise HTTPException(503, "governed-decision services not available")

        query = json.dumps(req.context, sort_keys=True, default=str)
        raw_output = json.dumps(
            {"decision": req.decision, "reason": req.reason},
            sort_keys=True, default=str,
        )

        # 1. Signed decision record.
        rec = decision_trail.log(
            tenant_id=tenant_id,
            store_id="governed",
            query=query,
            model_id=req.model_id,
            raw_output=raw_output,
            parameters={"invoice_id": req.invoice_id, "decision": req.decision},
            signing_identity=signing_identity,
        )

        # 2. Signed, hash-chained execution receipt (one chain per invoice).
        workflow_id = f"governed:{req.invoice_id or tenant_id}"
        try:
            step_number = len(execution_receipts.get_receipts(workflow_id))
        except Exception:
            step_number = 0
        receipt = execution_receipts.issue_receipt(
            tenant_id=tenant_id,
            step_id=uuid.uuid4().hex,
            workflow_id=workflow_id,
            step_number=step_number,
            input_text=query,
            retrieved_contents=[],
            governance_logs=[{"decision": req.decision, "reason": req.reason}],
            model_id=req.model_id,
            raw_output=raw_output,
            decision_id=rec.decision_id,
        )

        return {
            "decision_record": {
                "decision_id": rec.decision_id,
                "tenant_id": rec.tenant_id,
                "invoice_id": req.invoice_id,
                "decision": req.decision,
                "reason": req.reason,
                "model_id": rec.model_id,
                "raw_output": rec.raw_output,
                "created_at": rec.created_at.isoformat(),
                "signature": base64.b64encode(rec.signature).decode() if rec.signature else None,
                "public_key": base64.b64encode(rec.public_key).decode() if rec.public_key else None,
            },
            "execution_receipt": ExecutionReceiptService.receipt_to_dict(receipt),
        }

    return router


# ----------------------------------------------------------------------------
# GET /v1/gcrumbs/verify/key  +  POST /v1/gcrumbs/verify   (independent verifier)
# ----------------------------------------------------------------------------

class VerifyRequest(BaseModel):
    receipts: list[dict]
    public_key_b64: str | None = None   # if omitted, uses the key embedded in each receipt


def create_verify_router(signing_identity) -> APIRouter:
    router = APIRouter(prefix="/v1/gcrumbs", tags=["Independent Verification"])

    @router.get("/verify/key")
    def verify_key():
        if signing_identity is None:
            raise HTTPException(503, "no signing identity configured — receipts are unsigned")
        pub = signing_identity.public_key()
        return {
            "algorithm": "ed25519",
            "public_key_hex": pub.hex(),
            "public_key_b64": base64.b64encode(pub).decode(),
        }

    @router.post("/verify")
    def verify(req: VerifyRequest):
        if not req.receipts:
            return {"valid": False, "count": 0, "reason": "no receipts supplied", "results": []}
        results = []
        all_valid = True
        for i, r in enumerate(req.receipts):
            key = req.public_key_b64 or r.get("public_key_b64")
            valid, reason = ExecutionReceiptService.verify_receipt_dict(r, public_key_b64=key)
            results.append({
                "index": i,
                "receipt_id": r.get("receipt_id"),
                "valid": valid,
                "reason": reason,
            })
            all_valid = all_valid and valid
        return {"valid": all_valid, "count": len(results), "results": results}

    return router
