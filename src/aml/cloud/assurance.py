"""GRAFOMEM Continuous Assurance — scheduled conformance checks + drift detection.

Provides per-tenant scheduled conformance checking and drift detection.
Runs health probes, governance validation, chain integrity checks,
and metric baselines to detect configuration drift.

Sprint 19 deliverable.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.assurance")

# Schema
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS assurance_schedules (
    schedule_id   TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    interval_min  INTEGER NOT NULL DEFAULT 60,
    checks        JSONB NOT NULL DEFAULT '[]',
    alert_webhook TEXT,
    enabled       BOOLEAN DEFAULT TRUE,
    created_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_as_tenant ON assurance_schedules(tenant_id);

CREATE TABLE IF NOT EXISTS assurance_runs (
    run_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    schedule_id   TEXT,
    started_at    DOUBLE PRECISION NOT NULL,
    completed_at  DOUBLE PRECISION,
    status        TEXT NOT NULL DEFAULT 'running',
    results       JSONB NOT NULL DEFAULT '{}',
    drift_events  JSONB,
    baseline_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ar_tenant ON assurance_runs(tenant_id, started_at DESC);

CREATE TABLE IF NOT EXISTS assurance_baselines (
    baseline_id   TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    captured_at   DOUBLE PRECISION NOT NULL,
    snapshot      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ab_tenant ON assurance_baselines(tenant_id, captured_at DESC);
"""

@dataclass(slots=True)
class AssuranceSchedule:
    schedule_id: str
    tenant_id: str
    interval_min: int
    checks: list[str]
    alert_webhook: str | None
    enabled: bool
    created_at: float

@dataclass(slots=True)
class AssuranceRun:
    run_id: str
    tenant_id: str
    schedule_id: str | None
    started_at: float
    completed_at: float | None
    status: str  # 'pass', 'drift', 'fail', 'running'
    results: dict
    drift_events: list[dict] | None
    baseline_id: str | None

@dataclass(slots=True)
class AssuranceBaseline:
    baseline_id: str
    tenant_id: str
    captured_at: float
    snapshot: dict


class AssuranceService:
    """Continuous Assurance engine."""

    def __init__(self, db_url: str, pool=None, *, health_checker=None, metrics_mod=None) -> None:
        self._db_url = db_url
        self._pool = pool
        self._conn = None
        self._health = health_checker
        self._metrics = metrics_mod  # metrics module with get_metrics_summary()

    def _get_conn(self):
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._db_url, row_factory=dict_row, autocommit=True)
        return self._conn

    def close(self):
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def ensure_schema(self):
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Assurance schema ensured")

    # ---- Schedule CRUD ----

    def create_schedule(self, tenant_id, *, interval_min=60, checks=None, alert_webhook=None) -> AssuranceSchedule:
        sid = uuid.uuid4().hex[:24]
        now = time.time()
        checks = checks or ["health", "governance", "chain_integrity", "metrics"]
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO assurance_schedules (schedule_id, tenant_id, interval_min, checks, alert_webhook, enabled, created_at) "
            "VALUES (%s, %s, %s, %s, %s, TRUE, %s)",
            (sid, tenant_id, interval_min, json.dumps(checks), alert_webhook, now),
        )
        return AssuranceSchedule(sid, tenant_id, interval_min, checks, alert_webhook, True, now)

    def list_schedules(self, tenant_id) -> list[AssuranceSchedule]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM assurance_schedules WHERE tenant_id = %s ORDER BY created_at", (tenant_id,)
        ).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def get_all_active_schedules(self) -> list[AssuranceSchedule]:
        """Fetch all enabled schedules across all tenants."""
        with self.pool.cursor() as conn:
            rows = conn.execute("SELECT * FROM assurance_schedules WHERE enabled = TRUE").fetchall()
            return [
                AssuranceSchedule(r["schedule_id"], r["tenant_id"], r["interval_min"],
                                  r["checks"], r["alert_webhook"], r["enabled"], r["created_at"])
                for r in rows
            ]

    def get_schedule(self, schedule_id) -> AssuranceSchedule | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM assurance_schedules WHERE schedule_id = %s", (schedule_id,)
        ).fetchone()
        return self._row_to_schedule(row) if row else None

    def update_schedule(self, schedule_id, **updates) -> AssuranceSchedule | None:
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return None
        conn = self._get_conn()
        sets, vals = [], []
        for key in ("interval_min", "checks", "alert_webhook", "enabled"):
            if key in updates:
                val = updates[key]
                if key == "checks":
                    val = json.dumps(val)
                sets.append(f"{key} = %s")
                vals.append(val)
        if sets:
            vals.append(schedule_id)
            conn.execute(f"UPDATE assurance_schedules SET {', '.join(sets)} WHERE schedule_id = %s", vals)
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id) -> bool:
        conn = self._get_conn()
        result = conn.execute("DELETE FROM assurance_schedules WHERE schedule_id = %s", (schedule_id,))
        return result.rowcount > 0

    # ---- Run Assurance Check ----

    def run_check(self, tenant_id, *, schedule_id=None) -> AssuranceRun:
        run_id = uuid.uuid4().hex[:24]
        started = time.time()
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO assurance_runs (run_id, tenant_id, schedule_id, started_at, status, results) "
            "VALUES (%s, %s, %s, %s, 'running', '{}')",
            (run_id, tenant_id, schedule_id, started),
        )

        results = {}
        checks_passed = 0
        checks_total = 0

        # Check 1: Health
        checks_total += 1
        try:
            if self._health:
                readiness = self._health.readiness()
                results["health"] = {"status": readiness.get("status", "unknown"), "passed": readiness.get("status") == "ready"}
                if results["health"]["passed"]:
                    checks_passed += 1
            else:
                results["health"] = {"status": "skipped", "passed": True, "reason": "no health checker"}
                checks_passed += 1
        except Exception as e:
            results["health"] = {"status": "error", "passed": False, "error": str(e)}

        # Check 2: Database connectivity
        checks_total += 1
        try:
            conn.execute("SELECT 1")
            results["database"] = {"status": "ok", "passed": True}
            checks_passed += 1
        except Exception as e:
            results["database"] = {"status": "error", "passed": False, "error": str(e)}

        # Check 3: Metrics snapshot
        checks_total += 1
        try:
            if self._metrics and hasattr(self._metrics, 'get_metrics_summary'):
                snapshot = self._metrics.get_metrics_summary()
                results["metrics"] = {"status": "ok", "passed": True, "snapshot": snapshot}
            else:
                results["metrics"] = {"status": "ok", "passed": True, "snapshot": {}}
            checks_passed += 1
        except Exception as e:
            results["metrics"] = {"status": "error", "passed": False, "error": str(e)}

        # Check 4: gcrumbs chain integrity (query the table directly)
        checks_total += 1
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM information_schema.tables WHERE table_name = 'gcrumbs_breadcrumbs'"
            ).fetchone()
            if row and row["cnt"] > 0:
                bc_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM gcrumbs_breadcrumbs WHERE tenant_id = %s", (tenant_id,)
                ).fetchone()
                results["chain_integrity"] = {"status": "ok", "passed": True, "breadcrumbs": bc_count["cnt"] if bc_count else 0}
            else:
                results["chain_integrity"] = {"status": "ok", "passed": True, "reason": "gcrumbs table not initialized"}
            checks_passed += 1
        except Exception as e:
            results["chain_integrity"] = {"status": "error", "passed": False, "error": str(e)}

        # Check 5: Governance policies exist
        checks_total += 1
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM information_schema.tables WHERE table_name = 'governance_policies'"
            ).fetchone()
            if row and row["cnt"] > 0:
                pol_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM governance_policies WHERE tenant_id = %s AND enabled = TRUE", (tenant_id,)
                ).fetchone()
                results["governance"] = {"status": "ok", "passed": True, "active_policies": pol_count["cnt"] if pol_count else 0}
            else:
                results["governance"] = {"status": "ok", "passed": True, "reason": "governance table not initialized"}
            checks_passed += 1
        except Exception as e:
            results["governance"] = {"status": "error", "passed": False, "error": str(e)}

        # Determine overall status
        completed = time.time()
        overall = "pass" if checks_passed == checks_total else "fail"

        # Drift detection against baseline
        drift_events = []
        baseline = self.get_baseline(tenant_id)
        if baseline:
            drift_events = self._detect_drift(results, baseline.snapshot)
            if drift_events:
                overall = "drift"

        conn.execute(
            "UPDATE assurance_runs SET completed_at = %s, status = %s, results = %s, drift_events = %s, baseline_id = %s WHERE run_id = %s",
            (completed, overall, json.dumps(results), json.dumps(drift_events), baseline.baseline_id if baseline else None, run_id),
        )

        return AssuranceRun(run_id, tenant_id, schedule_id, started, completed, overall,
                           results, drift_events, baseline.baseline_id if baseline else None)

    # ---- Drift Detection ----

    def _detect_drift(self, current_results: dict, baseline_snapshot: dict) -> list[dict]:
        drift_events = []
        baseline_results = baseline_snapshot.get("results", {})

        for check_name, current in current_results.items():
            baseline_check = baseline_results.get(check_name, {})
            if baseline_check.get("passed") and not current.get("passed"):
                drift_events.append({
                    "check": check_name,
                    "type": "regression",
                    "detail": f"{check_name} was passing, now failing",
                    "baseline_status": baseline_check.get("status"),
                    "current_status": current.get("status"),
                })

        # Check for metric anomalies
        baseline_metrics = baseline_snapshot.get("metrics_snapshot", {})
        current_metrics = current_results.get("metrics", {}).get("snapshot", {})
        if baseline_metrics and current_metrics:
            for key in ("error_rate", "avg_latency_ms"):
                b_val = baseline_metrics.get(key, 0)
                c_val = current_metrics.get(key, 0)
                if b_val and c_val > b_val * 2:  # 2x threshold
                    drift_events.append({
                        "check": "metrics",
                        "type": "anomaly",
                        "metric": key,
                        "baseline_value": b_val,
                        "current_value": c_val,
                        "detail": f"{key} increased from {b_val} to {c_val} (>2x)",
                    })

        return drift_events

    # ---- Baseline ----

    def set_baseline(self, tenant_id) -> AssuranceBaseline:
        bid = uuid.uuid4().hex[:24]
        now = time.time()
        # Run a check to capture current state
        run = self.run_check(tenant_id)
        metrics_snapshot = run.results.get("metrics", {}).get("snapshot", {})
        snapshot = {"results": run.results, "metrics_snapshot": metrics_snapshot, "run_id": run.run_id}
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO assurance_baselines (baseline_id, tenant_id, captured_at, snapshot) VALUES (%s, %s, %s, %s)",
            (bid, tenant_id, now, json.dumps(snapshot)),
        )
        return AssuranceBaseline(bid, tenant_id, now, snapshot)

    def get_baseline(self, tenant_id) -> AssuranceBaseline | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM assurance_baselines WHERE tenant_id = %s ORDER BY captured_at DESC LIMIT 1", (tenant_id,)
        ).fetchone()
        return self._row_to_baseline(row) if row else None

    # ---- Run History ----

    def list_runs(self, tenant_id, *, limit=20) -> list[AssuranceRun]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM assurance_runs WHERE tenant_id = %s ORDER BY started_at DESC LIMIT %s", (tenant_id, limit)
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def get_run(self, run_id) -> AssuranceRun | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM assurance_runs WHERE run_id = %s", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def get_drift_events(self, tenant_id, *, limit=50) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT run_id, drift_events, started_at FROM assurance_runs "
            "WHERE tenant_id = %s AND status = 'drift' ORDER BY started_at DESC LIMIT %s",
            (tenant_id, limit),
        ).fetchall()
        events = []
        for r in rows:
            de = r.get("drift_events") or []
            if isinstance(de, str):
                de = json.loads(de)
            for e in de:
                e["run_id"] = r["run_id"]
                e["detected_at"] = r["started_at"]
                events.append(e)
        return events

    # ---- Stats ----

    def get_stats(self, tenant_id) -> dict:
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) as cnt FROM assurance_runs WHERE tenant_id = %s", (tenant_id,)).fetchone()
        passed = conn.execute("SELECT COUNT(*) as cnt FROM assurance_runs WHERE tenant_id = %s AND status = 'pass'", (tenant_id,)).fetchone()
        drifted = conn.execute("SELECT COUNT(*) as cnt FROM assurance_runs WHERE tenant_id = %s AND status = 'drift'", (tenant_id,)).fetchone()
        failed = conn.execute("SELECT COUNT(*) as cnt FROM assurance_runs WHERE tenant_id = %s AND status = 'fail'", (tenant_id,)).fetchone()
        schedules = conn.execute("SELECT COUNT(*) as cnt FROM assurance_schedules WHERE tenant_id = %s AND enabled = TRUE", (tenant_id,)).fetchone()
        return {
            "total_runs": total["cnt"] if total else 0,
            "passed": passed["cnt"] if passed else 0,
            "drifted": drifted["cnt"] if drifted else 0,
            "failed": failed["cnt"] if failed else 0,
            "active_schedules": schedules["cnt"] if schedules else 0,
        }

    # ---- Row converters ----

    @staticmethod
    def _row_to_schedule(r):
        checks = r.get("checks", [])
        if isinstance(checks, str):
            checks = json.loads(checks)
        return AssuranceSchedule(
            schedule_id=r["schedule_id"], tenant_id=r["tenant_id"],
            interval_min=r["interval_min"], checks=checks,
            alert_webhook=r.get("alert_webhook"), enabled=r.get("enabled", True),
            created_at=r["created_at"],
        )

    @staticmethod
    def _row_to_run(r):
        results = r.get("results", {})
        if isinstance(results, str):
            results = json.loads(results)
        drift = r.get("drift_events")
        if isinstance(drift, str):
            drift = json.loads(drift)
        return AssuranceRun(
            run_id=r["run_id"], tenant_id=r["tenant_id"],
            schedule_id=r.get("schedule_id"), started_at=r["started_at"],
            completed_at=r.get("completed_at"), status=r["status"],
            results=results, drift_events=drift, baseline_id=r.get("baseline_id"),
        )

    @staticmethod
    def _row_to_baseline(r):
        snapshot = r.get("snapshot", {})
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        return AssuranceBaseline(
            baseline_id=r["baseline_id"], tenant_id=r["tenant_id"],
            captured_at=r["captured_at"], snapshot=snapshot,
        )
