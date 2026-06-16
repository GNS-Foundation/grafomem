"""
GRAFOMEM Prometheus Metrics — instrumentation for observability.

Defines all Prometheus metrics, a Starlette middleware for automatic
HTTP request instrumentation, and the ``/metrics`` endpoint.

Metrics follow the Prometheus naming convention:
  - ``grafomem_`` prefix for all custom metrics
  - ``_total`` suffix for counters
  - ``_seconds`` suffix for duration histograms
  - ``_bytes`` suffix for size histograms

Usage:
    # In app.py:
    from aml.cloud.metrics import PrometheusMiddleware, metrics_endpoint
    app.add_middleware(PrometheusMiddleware)
    app.add_route("/metrics", metrics_endpoint, include_in_schema=False)

    # In business logic:
    from aml.cloud.metrics import GOVERNANCE_EVALUATIONS
    GOVERNANCE_EVALUATIONS.labels(result="deny").inc()
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("grafomem.cloud.metrics")

# ============================================================================
# Lazy Prometheus client import — metrics are NO-OPs if not installed
# ============================================================================

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        REGISTRY,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.info("prometheus-client not installed — metrics disabled")


# ============================================================================
# Metric definitions (created only if prometheus-client is available)
# ============================================================================

if PROMETHEUS_AVAILABLE:
    # --- HTTP request metrics (RED: Rate, Errors, Duration) ----------------

    HTTP_REQUESTS = Counter(
        "grafomem_http_requests_total",
        "Total HTTP requests",
        ["method", "path_template", "status"],
    )

    HTTP_DURATION = Histogram(
        "grafomem_http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method", "path_template", "status"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )

    HTTP_IN_PROGRESS = Gauge(
        "grafomem_http_requests_in_progress",
        "HTTP requests currently being processed",
        ["method"],
    )

    HTTP_RESPONSE_SIZE = Histogram(
        "grafomem_http_response_size_bytes",
        "HTTP response body size in bytes",
        ["method", "path_template"],
        buckets=(100, 500, 1000, 5000, 10000, 50000, 100000, 500000),
    )

    # --- Business metrics ---------------------------------------------------

    GOVERNANCE_EVALUATIONS = Counter(
        "grafomem_governance_evaluations_total",
        "Governance policy evaluations",
        ["result"],  # allow, deny, escalate
    )

    WORKFLOWS_TOTAL = Counter(
        "grafomem_workflows_total",
        "Workflow executions",
        ["status"],  # completed, failed, terminated
    )

    WORKFLOW_DURATION = Histogram(
        "grafomem_workflow_duration_seconds",
        "Workflow execution duration in seconds",
        ["mode"],  # sequential, parallel
        buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    )

    DECISIONS_LOGGED = Counter(
        "grafomem_decisions_logged_total",
        "Decision trail entries logged",
        ["model_id"],
    )

    TOKENS_CONSUMED = Counter(
        "grafomem_tokens_consumed_total",
        "LLM tokens consumed",
        ["model_id"],
    )

    MEMORY_OPERATIONS = Counter(
        "grafomem_memory_operations_total",
        "Memory store operations",
        ["operation"],  # write, retrieve, delete, supersede, batch_write
    )

    ERASURE_CERTIFICATES = Counter(
        "grafomem_erasure_certificates_total",
        "Erasure certificates issued",
    )

    WEBHOOKS_DISPATCHED = Counter(
        "grafomem_webhooks_dispatched_total",
        "Webhook delivery attempts",
        ["event_type", "success"],  # success: true/false
    )

    SSO_LOGINS = Counter(
        "grafomem_sso_logins_total",
        "SSO authentication events",
        ["provider"],
    )

    # --- Infrastructure gauges ----------------------------------------------

    DB_POOL_SIZE = Gauge(
        "grafomem_db_pool_size",
        "Database connection pool current size",
    )

    DB_POOL_AVAILABLE = Gauge(
        "grafomem_db_pool_available",
        "Database connections available in pool",
    )

    DB_POOL_WAITING = Gauge(
        "grafomem_db_pool_waiting",
        "Requests waiting for a database connection",
    )

    STORES_ACTIVE = Gauge(
        "grafomem_stores_active",
        "Number of active memory stores",
    )

    UPTIME_SECONDS = Gauge(
        "grafomem_uptime_seconds",
        "Server uptime in seconds",
    )

else:
    # Provide no-op stubs so business code doesn't need to check
    class _NoOpMetric:
        def labels(self, **kw): return self
        def inc(self, amount=1): pass
        def dec(self, amount=1): pass
        def set(self, value): pass
        def observe(self, value): pass

    HTTP_REQUESTS = _NoOpMetric()
    HTTP_DURATION = _NoOpMetric()
    HTTP_IN_PROGRESS = _NoOpMetric()
    HTTP_RESPONSE_SIZE = _NoOpMetric()
    GOVERNANCE_EVALUATIONS = _NoOpMetric()
    WORKFLOWS_TOTAL = _NoOpMetric()
    WORKFLOW_DURATION = _NoOpMetric()
    DECISIONS_LOGGED = _NoOpMetric()
    TOKENS_CONSUMED = _NoOpMetric()
    MEMORY_OPERATIONS = _NoOpMetric()
    ERASURE_CERTIFICATES = _NoOpMetric()
    WEBHOOKS_DISPATCHED = _NoOpMetric()
    SSO_LOGINS = _NoOpMetric()
    DB_POOL_SIZE = _NoOpMetric()
    DB_POOL_AVAILABLE = _NoOpMetric()
    DB_POOL_WAITING = _NoOpMetric()
    STORES_ACTIVE = _NoOpMetric()
    UPTIME_SECONDS = _NoOpMetric()


# ============================================================================
# Path template normalization
# ============================================================================

# Collapse UUIDs, numeric IDs, and other variable path segments
_PATH_PATTERNS = [
    (re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "/{id}"),
    (re.compile(r"/[0-9a-f]{24,}"), "/{id}"),
    (re.compile(r"/\d+"), "/{id}"),
]

# Paths to exclude from metrics (high cardinality or static files)
_EXCLUDED_PATHS = {"/metrics", "/observability/metrics", "/healthz", "/readyz", "/docs", "/redoc", "/openapi.json"}


def _normalize_path(path: str) -> str:
    """Normalize a URL path to a template for metric labels.

    Replaces UUIDs, hex IDs, and numeric segments with ``{id}``
    to prevent label cardinality explosion.
    """
    if path in _EXCLUDED_PATHS:
        return path
    for pattern, replacement in _PATH_PATTERNS:
        path = pattern.sub(replacement, path)
    return path


# ============================================================================
# Prometheus middleware
# ============================================================================

class PrometheusMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that instruments HTTP requests with Prometheus metrics.

    Records request count, duration, in-progress gauge, and response size
    for every request. Static file and health check paths are excluded.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Skip static files and infrastructure endpoints
        if path.startswith("/portal") or path.startswith("/landing") or path.startswith("/static"):
            return await call_next(request)

        method = request.method
        path_template = _normalize_path(path)

        HTTP_IN_PROGRESS.labels(method=method).inc()
        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            HTTP_IN_PROGRESS.labels(method=method).dec()
            HTTP_REQUESTS.labels(method=method, path_template=path_template, status="500").inc()
            raise

        duration = time.perf_counter() - start
        status = str(response.status_code)

        HTTP_REQUESTS.labels(method=method, path_template=path_template, status=status).inc()
        HTTP_DURATION.labels(method=method, path_template=path_template, status=status).observe(duration)
        HTTP_IN_PROGRESS.labels(method=method).dec()

        # Response size (if available)
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                HTTP_RESPONSE_SIZE.labels(method=method, path_template=path_template).observe(int(content_length))
            except (ValueError, TypeError):
                pass

        return response


# ============================================================================
# /metrics endpoint
# ============================================================================

async def metrics_endpoint(request: Request) -> Response:
    """Prometheus metrics endpoint — returns exposition format.

    Registered at ``/metrics`` without auth so Prometheus can scrape it.
    """
    if not PROMETHEUS_AVAILABLE:
        return Response(
            content="# prometheus-client not installed\n",
            media_type="text/plain",
            status_code=503,
        )

    # Update infrastructure gauges before generating output
    _update_infrastructure_gauges(request.app)

    body = generate_latest(REGISTRY)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)


def _update_infrastructure_gauges(app) -> None:
    """Refresh infrastructure gauges from app state."""
    # Pool stats
    pool = getattr(app.state, "db_pool", None)
    if pool is not None and pool.is_active:
        s = pool.stats
        DB_POOL_SIZE.set(s.get("pool_size", 0))
        DB_POOL_AVAILABLE.set(s.get("pool_available", 0))
        DB_POOL_WAITING.set(s.get("requests_waiting", 0))

    # Store count
    sm = getattr(app.state, "store_manager", None)
    if sm is not None and hasattr(sm, "_stores"):
        STORES_ACTIVE.set(len(sm._stores))

    # Uptime
    from aml.cloud.health import _START_TIME
    UPTIME_SECONDS.set(time.monotonic() - _START_TIME)


# ============================================================================
# Metrics summary (for portal /v1/monitoring/stats)
# ============================================================================

def get_metrics_summary() -> dict[str, Any]:
    """Return a JSON-friendly summary of key Prometheus metrics.

    Used by the portal monitoring tab — not the same as the
    ``/metrics`` exposition format.
    """
    if not PROMETHEUS_AVAILABLE:
        return {"available": False}

    def _counter_value(counter, labels=None) -> float:
        """Get the current value of a counter (with optional labels)."""
        try:
            if labels:
                return counter.labels(**labels)._value.get()
            # Sum across all label combinations
            total = 0.0
            for metric in counter.collect():
                for sample in metric.samples:
                    if sample.name.endswith("_total"):
                        total += sample.value
            return total
        except Exception:
            return 0.0

    return {
        "available": True,
        "governance": {
            "allow": _counter_value(GOVERNANCE_EVALUATIONS, {"result": "allow"}),
            "deny": _counter_value(GOVERNANCE_EVALUATIONS, {"result": "deny"}),
            "escalate": _counter_value(GOVERNANCE_EVALUATIONS, {"result": "escalate"}),
        },
        "workflows": {
            "completed": _counter_value(WORKFLOWS_TOTAL, {"status": "completed"}),
            "failed": _counter_value(WORKFLOWS_TOTAL, {"status": "failed"}),
            "terminated": _counter_value(WORKFLOWS_TOTAL, {"status": "terminated"}),
        },
        "memory_operations": _counter_value(MEMORY_OPERATIONS),
        "decisions_logged": _counter_value(DECISIONS_LOGGED),
        "erasure_certificates": _counter_value(ERASURE_CERTIFICATES),
        "webhooks_dispatched": _counter_value(WEBHOOKS_DISPATCHED),
        "sso_logins": _counter_value(SSO_LOGINS),
    }
