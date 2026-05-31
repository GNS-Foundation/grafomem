"""GRAFOMEM Continuous Assurance Tests — Sprint 19.

DB-free tests verifying assurance service data models,
drift detection logic, row converters, and scheduler lifecycle.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aml.cloud.assurance import (
    AssuranceBaseline,
    AssuranceRun,
    AssuranceSchedule,
    AssuranceService,
)
from aml.cloud.scheduler import AssuranceScheduler


# ---- Data Model Tests ----


def test_schedule_dataclass():
    s = AssuranceSchedule(
        schedule_id="s-1",
        tenant_id="t-1",
        interval_min=60,
        checks=["health", "governance"],
        alert_webhook=None,
        enabled=True,
        created_at=1000.0,
    )
    assert s.schedule_id == "s-1"
    assert s.interval_min == 60
    assert len(s.checks) == 2


def test_run_dataclass():
    r = AssuranceRun(
        run_id="r-1",
        tenant_id="t-1",
        schedule_id=None,
        started_at=1000.0,
        completed_at=1001.0,
        status="pass",
        results={"health": {"passed": True}},
        drift_events=None,
        baseline_id=None,
    )
    assert r.status == "pass"
    assert r.results["health"]["passed"] is True


def test_baseline_dataclass():
    b = AssuranceBaseline(
        baseline_id="b-1",
        tenant_id="t-1",
        captured_at=1000.0,
        snapshot={"results": {}},
    )
    assert b.snapshot == {"results": {}}


# ---- Drift Detection Tests ----


def test_detect_drift_regression():
    svc = AssuranceService.__new__(AssuranceService)
    current = {"health": {"passed": False, "status": "error"}}
    baseline = {"results": {"health": {"passed": True, "status": "ok"}}}
    drift = svc._detect_drift(current, baseline)
    assert len(drift) == 1
    assert drift[0]["type"] == "regression"
    assert drift[0]["check"] == "health"


def test_detect_drift_no_regression():
    svc = AssuranceService.__new__(AssuranceService)
    current = {"health": {"passed": True, "status": "ok"}}
    baseline = {"results": {"health": {"passed": True, "status": "ok"}}}
    drift = svc._detect_drift(current, baseline)
    assert len(drift) == 0


def test_detect_drift_metric_anomaly():
    svc = AssuranceService.__new__(AssuranceService)
    current = {"metrics": {"snapshot": {"error_rate": 0.15, "avg_latency_ms": 500}}}
    baseline = {
        "results": {},
        "metrics_snapshot": {"error_rate": 0.05, "avg_latency_ms": 100},
    }
    drift = svc._detect_drift(current, baseline)
    assert any(d["type"] == "anomaly" for d in drift)


def test_detect_drift_no_baseline_metrics():
    svc = AssuranceService.__new__(AssuranceService)
    current = {"health": {"passed": True}}
    baseline = {"results": {"health": {"passed": True}}}
    drift = svc._detect_drift(current, baseline)
    assert len(drift) == 0


# ---- Row Converter Tests ----


def test_row_to_schedule():
    row = {
        "schedule_id": "s-1",
        "tenant_id": "t-1",
        "interval_min": 30,
        "checks": ["health"],
        "alert_webhook": "https://hook.example.com",
        "enabled": True,
        "created_at": 1000.0,
    }
    s = AssuranceService._row_to_schedule(row)
    assert s.interval_min == 30
    assert s.alert_webhook == "https://hook.example.com"


def test_row_to_schedule_json_string_checks():
    row = {
        "schedule_id": "s-2",
        "tenant_id": "t-1",
        "interval_min": 60,
        "checks": '["health", "governance"]',
        "alert_webhook": None,
        "enabled": True,
        "created_at": 1000.0,
    }
    s = AssuranceService._row_to_schedule(row)
    assert s.checks == ["health", "governance"]


def test_row_to_run():
    row = {
        "run_id": "r-1",
        "tenant_id": "t-1",
        "schedule_id": None,
        "started_at": 1000.0,
        "completed_at": 1001.0,
        "status": "drift",
        "results": {"health": {"passed": False}},
        "drift_events": [{"check": "health", "type": "regression"}],
        "baseline_id": "b-1",
    }
    r = AssuranceService._row_to_run(row)
    assert r.status == "drift"
    assert len(r.drift_events) == 1


def test_row_to_baseline():
    row = {
        "baseline_id": "b-1",
        "tenant_id": "t-1",
        "captured_at": 1000.0,
        "snapshot": '{"results": {}}',
    }
    b = AssuranceService._row_to_baseline(row)
    assert b.snapshot == {"results": {}}


# ---- Scheduler Tests ----


def test_scheduler_start_stop():
    mock_svc = MagicMock()
    scheduler = AssuranceScheduler(mock_svc)
    asyncio.run(scheduler.start())
    assert scheduler._running is True
    asyncio.run(scheduler.stop())
    assert scheduler._running is False


def test_scheduler_status():
    mock_svc = MagicMock()
    scheduler = AssuranceScheduler(mock_svc)
    status = scheduler.get_status()
    assert status["running"] is False
    assert status["active_schedules"] == 0


def test_scheduler_active_count():
    mock_svc = MagicMock()
    scheduler = AssuranceScheduler(mock_svc)
    assert scheduler.active_count == 0
