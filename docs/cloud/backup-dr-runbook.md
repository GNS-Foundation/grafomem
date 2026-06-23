# GRAFOMEM Cloud — Backup and Disaster Recovery Runbook

This runbook establishes the baseline Disaster Recovery (DR) posture for GRAFOMEM Cloud deployments. It defines our Recovery Point Objective (RPO) and Recovery Time Objective (RTO), explains our multi-region data residency strategy, and outlines the strict operational procedure for restoring a database snapshot while preserving cryptographic "Right-to-be-Forgotten" integrity.

---

## 1. Resilience Targets

### RPO: 5 Minutes (Continuous WAL Shipping)
Our architecture mandates continuous Write-Ahead Log (WAL) shipping for all PostgreSQL production shards. This limits data loss during a catastrophic failure to a maximum of 5 minutes.

### RTO: 1 Hour (Instance Recovery)
In the event of a full zonal or regional outage, the target time to restore service (using Point-In-Time-Recovery from WAL archives) is 1 hour. This applies to spinning up a new DB instance, applying the WAL, and switching traffic.

---

## 2. Data Residency & Multi-Region Sharding

To satisfy stringent DORA (Digital Operational Resilience Act) and GDPR compliance requirements for European financial and healthcare institutions:
- **Dedicated Shards:** Production PostgreSQL databases are sharded by region. A tenant provisioned in `eu-central-1` will never have their data cross borders to `us-east-1`.
- **Read Replicas:** Read replicas follow the same geographic constraints. Cross-region replication is strictly disabled for EU-bound tenants.
- **Failover:** If `eu-central-1` falls, we fail over to `eu-west-1` or `eu-west-3` (Paris/Ireland) to maintain the data residency boundary.

---

## 3. Snapshot Restoration Procedure

Restoring a database from a snapshot inherently risks rolling back the clock on data deletions. If a user exercised their right to be forgotten *after* the snapshot was taken, restoring that snapshot will illegally "resurrect" their data. 

To prevent this, GRAFOMEM mandates the **Restore-then-W6 Probe** protocol.

### Step 3.1: Provision and Restore
1. Provision an isolated recovery database instance.
2. Apply the snapshot and any necessary WAL archives up to the desired recovery point.

### Step 3.2: Run the W6 Erasure Probe (Mandatory)
Before the restored instance is allowed to accept live production traffic, it must pass a cryptographic validation against the immutable `erasure_certificates` ledger.

Run the verification probe from the operations terminal:
```bash
python scripts/verify_restore_probe.py --db-url "postgresql://postgres:password@recovery-host:5432/grafomem"
```

**What the probe does:**
1. It reads every `certificate_id` issued in the system.
2. It asserts `M1 recall over survivor-probes` (W6 primitive logic).
3. If it finds a single memory reference (`ref`) in `memory_embeddings` or `memories` that was supposedly erased, **the probe will fatally fail**.

### Step 3.3: Scrub and Cutover
If the probe detects resurrections (which happens if you restore to a point *before* the erasures took place), you must re-run the `ErasureSweeper` daemon across the restored dataset to automatically re-delete any records that were restored but carry an active erasure certificate.

Only when `verify_restore_probe.py` returns `PASS (NO LEAKS)` is the instance certified to join the production routing pool.
