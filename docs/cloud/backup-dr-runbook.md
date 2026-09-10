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

> **⚠ WITHDRAWN 2026-09-10 — this section does not deliver what it claims.**
> Per the erasure-ledger incident of 2026-09-10 (recorded in `grafomem-internal`),
> the protocol below **cannot currently prevent resurrection**, for two
> independent reasons:
>
> 1. **The probe validates against the restored database itself.** Step 3.2 reads
>    `erasure_certificates` from the recovery instance — but certificates issued
>    *after* the snapshot point are missing from exactly the dataset being
>    validated. The probe therefore passes on the very resurrections it exists to
>    catch. Validation must run against an erasure record that is **independent
>    of the database being restored**; no such populated, independent record
>    exists today.
> 2. **The independent record that was designed for this — `erasure_ledger` — is
>    empty and co-located.** It lives in the same database, and its pool has been
>    unable to authenticate since at least the earliest retained logs; 14 signed
>    certificates issued after 2026-06-22 have no ledger row.
>
> Until the ledger is relocated to a store independent of the primary database
> and backfilled, treat a restore as **unable to automatically re-apply
> post-snapshot erasures**. The manual mitigation is Step 3.3's sweep driven by
> whatever certificate evidence exists *outside* the restored instance (e.g. the
> current production `erasure_certificates` table, exported before cutover).
> This notice is dated and stays until relocation lands.

Restoring a database from a snapshot inherently risks rolling back the clock on data deletions. If a user exercised their right to be forgotten *after* the snapshot was taken, restoring that snapshot will illegally "resurrect" their data. 

To prevent this, GRAFOMEM intended the **Restore-then-W6 Probe** protocol (see withdrawal above):

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
