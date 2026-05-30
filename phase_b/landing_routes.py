"""
src/aml/cloud/landing_routes.py   (PHASE-B SKELETON)

/v1/landing/ — mirrors the /v1/erasure/ router shape. ADAPT the two integration points:
  - how the tenant is resolved from auth (X-API-Key middleware)
  - how the LandingService instance is reached (app.state vs dependency)
"""
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

# ADAPT: import your real service + errors
from .landing_service import LandingService, LandingIssueRequest, LandingDenied, LandingPendingHITL, LandingError

router = APIRouter(prefix="/v1/landing", tags=["landing"])


def _svc(request: Request) -> LandingService:
    # ADAPT: services are initialized in app.py lifespan; reach the instance however your app does.
    return request.app.state.landing_service

def _tenant(request: Request) -> str:
    # ADAPT: your auth middleware already resolves X-API-Key -> tenant_id; read it here.
    return request.state.tenant_id


class ConformanceBody(BaseModel):
    artifact_ref: str
    layer_hashes: list
    data_provenance: dict

class IssueBody(BaseModel):
    artifact_ref: str
    base_model_ref: str
    layer_hashes: list
    data_provenance: dict
    authority: dict
    conformance: dict
    permitted_actions: list
    kind: str = "lora+rag"

class ResumeBody(BaseModel):
    approver: str


@router.post("/conformance")
def run_conformance(body: ConformanceBody, request: Request):
    svc = _svc(request)
    # NOTE: layer_bytes aren't sent over the wire — pass hashes; conformance checks structure here.
    return svc.run_conformance(_tenant(request), body.artifact_ref, [], body.data_provenance)

@router.post("/certificates", status_code=201)
def issue(body: IssueBody, request: Request):
    svc = _svc(request)
    req = LandingIssueRequest(**body.model_dump())
    try:
        return svc.issue_certificate(_tenant(request), req)
    except LandingDenied as e:
        raise HTTPException(403, detail={"status": "denied", "certificate_id": str(e)})
    except LandingPendingHITL as e:
        # 202: parked for human approval (resume via /approve)
        raise HTTPException(202, detail={"status": "waiting_hitl", "certificate_id": e.certificate_id})
    except LandingError as e:
        raise HTTPException(400, detail=str(e))

@router.get("/certificates/{certificate_id}")
def get_one(certificate_id: str, request: Request):
    try:
        return _svc(request).get_certificate(_tenant(request), certificate_id)
    except LandingError:
        raise HTTPException(404, detail="certificate not found")

@router.get("/certificates")
def list_all(request: Request, limit: int = Query(50, le=200), offset: int = 0):
    return _svc(request).list_certificates(_tenant(request), limit=limit, offset=offset)

@router.post("/certificates/{certificate_id}/verify")
def verify(certificate_id: str, request: Request):
    return _svc(request).verify_certificate(_tenant(request), certificate_id)

@router.post("/certificates/{certificate_id}/approve")
def approve(certificate_id: str, body: ResumeBody, request: Request):
    return _svc(request).resume(_tenant(request), certificate_id, True, body.approver)

@router.post("/certificates/{certificate_id}/reject")
def reject(certificate_id: str, body: ResumeBody, request: Request):
    return _svc(request).resume(_tenant(request), certificate_id, False, body.approver)
