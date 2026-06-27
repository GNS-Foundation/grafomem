"""GRAFOMEM Continuous Assurance API routes.

Provides REST endpoints for managing assurance schedules,
triggering manual checks, viewing run history, drift events,
and capturing baselines.

Sprint 19 deliverable.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from aml.server.scopes import require_scope

logger = logging.getLogger("grafomem.cloud.assurance_routes")

router = APIRouter(prefix="/v1/assurance", tags=["assurance"])

# ---- Request Models ----

class CreateScheduleRequest(BaseModel):
    interval_min: int = Field(default=60, ge=5, le=1440, description="Check interval in minutes")
    checks: list[str] = Field(default=["health", "governance", "chain_integrity", "metrics"])
    alert_webhook: str | None = None

class UpdateScheduleRequest(BaseModel):
    interval_min: int | None = None
    checks: list[str] | None = None
    alert_webhook: str | None = None
    enabled: bool | None = None

# ---- Helper ----

def _get_assurance(request: Request):
    svc = getattr(request.app.state, "assurance_service", None)
    if svc is None:
        raise HTTPException(503, "Assurance service not available")
    return svc

def _get_tenant(request: Request) -> str:
    return getattr(request.state, "tenant_id", "default")

# ---- Endpoints ----

@router.post("/schedules")
def create_schedule(body: CreateScheduleRequest, request: Request):
    svc = _get_assurance(request)
    tenant = _get_tenant(request)
    require_scope(request, "compliance:admin")
    schedule = svc.create_schedule(
        tenant, interval_min=body.interval_min,
        checks=body.checks, alert_webhook=body.alert_webhook,
    )
    
    # Notify scheduler
    scheduler = getattr(request.app.state, "assurance_scheduler", None)
    if scheduler:
        scheduler.schedule(schedule.schedule_id, tenant, schedule.interval_min)
        
    return {"schedule_id": schedule.schedule_id, "interval_min": schedule.interval_min,
            "checks": schedule.checks, "enabled": schedule.enabled}

@router.get("/schedules")
def list_schedules(request: Request):
    svc = _get_assurance(request)
    tenant = _get_tenant(request)
    require_scope(request, "compliance:read")
    schedules = svc.list_schedules(tenant)
    return {"schedules": [{
        "schedule_id": s.schedule_id, "interval_min": s.interval_min,
        "checks": s.checks, "enabled": s.enabled,
        "alert_webhook": s.alert_webhook, "created_at": s.created_at,
    } for s in schedules]}

@router.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: str, body: UpdateScheduleRequest, request: Request):
    svc = _get_assurance(request)
    require_scope(request, "compliance:admin")
    updates = body.model_dump(exclude_none=True)
    schedule = svc.update_schedule(schedule_id, **updates)
    if not schedule:
        raise HTTPException(404, "Schedule not found")
        
    # Notify scheduler
    scheduler = getattr(request.app.state, "assurance_scheduler", None)
    if scheduler:
        if schedule.enabled:
            scheduler.schedule(schedule.schedule_id, schedule.tenant_id, schedule.interval_min)
        else:
            scheduler.unschedule(schedule.schedule_id)
            
    return {"schedule_id": schedule.schedule_id, "interval_min": schedule.interval_min,
            "checks": schedule.checks, "enabled": schedule.enabled}

@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str, request: Request):
    svc = _get_assurance(request)
    require_scope(request, "compliance:admin")
    deleted = svc.delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(404, "Schedule not found")
        
    # Notify scheduler
    scheduler = getattr(request.app.state, "assurance_scheduler", None)
    if scheduler:
        scheduler.unschedule(schedule_id)
        
    return {"deleted": True}

@router.post("/run")
def trigger_run(request: Request):
    svc = _get_assurance(request)
    tenant = _get_tenant(request)
    require_scope(request, "compliance:admin")
    run = svc.run_check(tenant)
    return {
        "run_id": run.run_id, "status": run.status,
        "checks_results": run.results, "drift_events": run.drift_events or [],
        "started_at": run.started_at, "completed_at": run.completed_at,
    }

@router.get("/runs")
def list_runs(request: Request, limit: int = 20):
    svc = _get_assurance(request)
    tenant = _get_tenant(request)
    require_scope(request, "compliance:read")
    runs = svc.list_runs(tenant, limit=limit)
    return {"runs": [{
        "run_id": r.run_id, "status": r.status,
        "started_at": r.started_at, "completed_at": r.completed_at,
        "drift_events_count": len(r.drift_events) if r.drift_events else 0,
    } for r in runs]}

@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    svc = _get_assurance(request)
    require_scope(request, "compliance:read")
    run = svc.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return {
        "run_id": run.run_id, "tenant_id": run.tenant_id,
        "status": run.status, "results": run.results,
        "drift_events": run.drift_events or [],
        "started_at": run.started_at, "completed_at": run.completed_at,
        "baseline_id": run.baseline_id,
    }

@router.post("/baseline")
def capture_baseline(request: Request):
    svc = _get_assurance(request)
    tenant = _get_tenant(request)
    require_scope(request, "compliance:admin")
    baseline = svc.set_baseline(tenant)
    return {
        "baseline_id": baseline.baseline_id,
        "captured_at": baseline.captured_at,
        "snapshot_keys": list(baseline.snapshot.keys()),
    }

@router.get("/baseline")
def get_baseline(request: Request):
    svc = _get_assurance(request)
    tenant = _get_tenant(request)
    require_scope(request, "compliance:read")
    baseline = svc.get_baseline(tenant)
    if not baseline:
        raise HTTPException(404, "No baseline captured")
    return {
        "baseline_id": baseline.baseline_id,
        "captured_at": baseline.captured_at,
        "snapshot": baseline.snapshot,
    }

@router.get("/drift")
def get_drift_events(request: Request, limit: int = 50):
    svc = _get_assurance(request)
    tenant = _get_tenant(request)
    require_scope(request, "compliance:read")
    events = svc.get_drift_events(tenant, limit=limit)
    return {"drift_events": events, "count": len(events)}

@router.get("/stats")
def get_stats(request: Request):
    svc = _get_assurance(request)
    tenant = _get_tenant(request)
    require_scope(request, "compliance:read")
    return svc.get_stats(tenant)
