"""
GRAFOMEM Health Checker — liveness, readiness, and system stats.

Provides health check logic used by:
  - ``/healthz``  — Kubernetes liveness probe (always 200 if server is up)
  - ``/readyz``   — Kubernetes readiness probe (checks DB, pool, services)
  - ``/v1/monitoring/stats`` — Full system stats for portal dashboard

All health endpoints are registered WITHOUT auth middleware so that
infrastructure probes (Kubernetes, Railway, load balancers) can reach them.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("grafomem.cloud.health")

# Server start time — set once at import
_START_TIME = time.monotonic()
_START_DATETIME = datetime.now(timezone.utc)


class HealthChecker:
    """System health checker with dependency probes.

    Parameters
    ----------
    app : FastAPI
        The application instance (used to access ``app.state``).
    db_url : str | None
        Database URL for direct connectivity checks.
    """

    def __init__(self, app, db_url: str | None = None) -> None:
        self._app = app
        self._db_url = db_url

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - _START_TIME

    # ------------------------------------------------------------------
    # Liveness — server is alive
    # ------------------------------------------------------------------

    def liveness(self) -> dict[str, Any]:
        """Liveness check — 200 if the process is running."""
        return {
            "status": "ok",
            "uptime_seconds": round(self.uptime_seconds, 1),
            "started_at": _START_DATETIME.isoformat(),
            "version": getattr(self._app, "version", "unknown"),
        }

    # ------------------------------------------------------------------
    # Readiness — all dependencies available
    # ------------------------------------------------------------------

    def readiness(self) -> dict[str, Any]:
        """Readiness check — verifies downstream dependencies."""
        checks: dict[str, dict[str, Any]] = {}

        # Database connectivity
        checks["database"] = self._check_database()

        # Connection pool
        checks["pool"] = self._check_pool()

        # Store manager
        checks["store_manager"] = self._check_store_manager()

        # Core services
        for svc_name in (
            "decision_trail", "governance_gateway", "orchestrator",
            "erasure_proof", "regulatory_reports",
        ):
            svc = getattr(self._app.state, svc_name, None)
            if svc is not None:
                checks[svc_name] = {"status": "ok"}
            # Don't fail readiness for missing optional services

        all_ok = all(c.get("status") == "ok" for c in checks.values())
        return {
            "status": "ok" if all_ok else "degraded",
            "checks": checks,
            "uptime_seconds": round(self.uptime_seconds, 1),
        }

    # ------------------------------------------------------------------
    # Full stats — for portal monitoring tab
    # ------------------------------------------------------------------

    def full_stats(self) -> dict[str, Any]:
        """Comprehensive system stats for the monitoring dashboard."""
        stats: dict[str, Any] = {
            "status": "ok",
            "uptime_seconds": round(self.uptime_seconds, 1),
            "started_at": _START_DATETIME.isoformat(),
            "version": getattr(self._app, "version", "unknown"),
        }

        # Pool stats
        pool = getattr(self._app.state, "db_pool", None)
        if pool is not None:
            stats["pool"] = pool.stats
        else:
            stats["pool"] = {"pooled": False}

        # Store count
        sm = getattr(self._app.state, "store_manager", None)
        if sm is not None:
            stats["stores"] = {
                "active": len(sm._stores) if hasattr(sm, "_stores") else 0,
            }

        # Prometheus metrics summary (if available)
        try:
            from aml.cloud.metrics import get_metrics_summary
            stats["metrics"] = get_metrics_summary()
        except Exception:
            stats["metrics"] = {}

        return stats

    # ------------------------------------------------------------------
    # Dependency checks
    # ------------------------------------------------------------------

    def _check_database(self) -> dict[str, Any]:
        """Check database connectivity with a simple SELECT 1."""
        if not self._db_url:
            return {"status": "ok", "detail": "no database configured"}
        try:
            pool = getattr(self._app.state, "db_pool", None)
            if pool is not None and pool.is_active:
                with pool.connection() as conn:
                    conn.execute("SELECT 1")
                return {"status": "ok", "via": "pool"}
            else:
                import psycopg
                conn = psycopg.connect(self._db_url, autocommit=True)
                conn.execute("SELECT 1")
                conn.close()
                return {"status": "ok", "via": "direct"}
        except Exception as e:
            logger.warning("Database health check failed: %s", e)
            return {"status": "error", "detail": str(e)}

    def _check_pool(self) -> dict[str, Any]:
        """Check connection pool status."""
        pool = getattr(self._app.state, "db_pool", None)
        if pool is None:
            return {"status": "ok", "detail": "no pool configured"}
        if not pool.is_active:
            return {"status": "error", "detail": "pool not active"}
        s = pool.stats
        return {
            "status": "ok",
            "size": s.get("pool_size", 0),
            "available": s.get("pool_available", 0),
            "waiting": s.get("requests_waiting", 0),
        }

    def _check_store_manager(self) -> dict[str, Any]:
        """Check store manager is available."""
        sm = getattr(self._app.state, "store_manager", None)
        if sm is None:
            return {"status": "error", "detail": "store manager not initialized"}
        count = len(sm._stores) if hasattr(sm, "_stores") else 0
        return {"status": "ok", "active_stores": count}
