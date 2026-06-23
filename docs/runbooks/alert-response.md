# GRAFOMEM Cloud — Alert Response Runbook

This playbook provides standard operating procedures (SOPs) for responding to the Prometheus alerts defined in `monitoring/prometheus-alerts.yml`.

---

## 1. HighAPIErrorRate

**Severity:** Critical  
**Condition:** > 1% of API requests return 5xx over 5 minutes.

### Triage Steps
1. **Check Railway Metrics:** Open the Railway dashboard and check the CPU/Memory consumption of the `grafomem-web` service. If OOM (Out Of Memory) killed, increase the RAM limit.
2. **Check Postgres:** Ensure the database is not connection-starved or locking up.
3. **Rollback:** If this alert fired immediately following a deployment, trigger an immediate rollback to the previous Railway build.

---

## 2. APILatencySpike

**Severity:** Warning  
**Condition:** p95 latency > 250ms for 10 minutes.

### Triage Steps
1. **Identify the Endpoint:** Use the Prometheus dashboard to group `grafomem_http_request_duration_seconds` by `path_template` and `method`. 
2. **Check Embedding Provider:** If the slow endpoint is `/v1/memories` (POST), check the latency of the backend embedding provider (e.g., OpenAI or local BGE). 
3. **Scale Replicas:** If the bottleneck is purely compute-bound, increase the replica count of the `grafomem-web` service in Railway.

---

## 3. ErasureSweepFailed

**Severity:** Critical (PagerDuty Immediate)  
**Condition:** `grafomem_erasure_sweep_errors_total` increments.

### Why this is critical
This alert means the asynchronous `ErasureSweeper` daemon failed to process pending data deletions. This blocks the Right-to-be-Forgotten pipeline and puts GRAFOMEM in direct violation of GDPR/DORA compliance if not resolved within the permitted window.

### Triage Steps
1. **Check Daemon Logs:** Go to the Railway dashboard for the `ErasureSweeper` worker and read the logs.
2. **Postgres Connectivity:** Ensure the `GRAFOMEM_DB_URL` is correct and the daemon has network access to `postgres.railway.internal`.
3. **Schema Migrations:** If the logs show `psycopg.errors.UndefinedColumn`, ensure that all Protocol 3.4 schema migrations (e.g., `002_w9_erasure.sql`) have been applied to the production database.
4. **Restart Daemon:** After fixing the root cause, restart the Railway service. The daemon will automatically resume processing the `erasure_pending` queue from where it left off.

---

## 4. HighWorkflowErrorRate

**Severity:** Warning  
**Condition:** > 1% of orchestrator steps fail over 30 minutes.

### Triage Steps
1. **Check LLM Provider:** Most workflow failures stem from transient LLM API errors (rate limits, timeouts). Check the upstream provider status page.
2. **Review Dead-Letter Queue:** Investigate failed steps in the `orchestrator_steps` table where `status = 'failed'`.
