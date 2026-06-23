# GRAFOMEM Cloud — Service Level Objectives (SLOs)

This document formalizes the enterprise commitments for the GRAFOMEM Cloud platform. These Service Level Objectives (SLOs) represent the strict thresholds we guarantee for our customers, backed by continuous Prometheus telemetry.

---

## 1. Availability

**Objective:** 99.9% Uptime for the primary API.

**Definition:** The API is considered "available" if it responds to well-formed `HTTP GET` and `POST` requests with a `2xx` or `4xx` status code. Server errors (`5xx`) count against the error budget.

**Measurement:** Evaluated over a rolling 30-day window using the Prometheus `grafomem_http_requests_total` metric.

---

## 2. Latency

**Objective:** p95 Latency < 250ms for synchronous API requests.

**Definition:** 95% of all synchronous read (`GET`) and write (`POST`) operations to the `/v1/memories` and `/v1/decisions` endpoints must complete in under 250 milliseconds.

**Measurement:** Measured via the `grafomem_http_request_duration_seconds` histogram in Prometheus. Background processing (like background erasure sweeps and heavy embedding generation for bulk uploads) is excluded from this metric.

---

## 3. Cryptographic Erasure Compliance

**Objective:** 100% W9 Erasure Sweep Success Rate.

**Definition:** The `ErasureSweeper` daemon must successfully process all `erasure_pending` flags without encountering database disconnects, integrity failures, or timeouts. A single sweep failure indicates a potential Right-to-be-Forgotten pipeline violation.

**Measurement:** Monitored via the `grafomem_erasure_sweep_errors_total` metric. **This is a zero-tolerance metric.**

---

## 4. Background Workflow Reliability

**Objective:** < 1% Error Rate on Background Workflows.

**Definition:** Orchestrator steps and background inference tasks must complete successfully.

**Measurement:** Evaluated by observing the `grafomem_workflows_total{status="failed"}` counter against the total workflow count over a 24-hour period.
