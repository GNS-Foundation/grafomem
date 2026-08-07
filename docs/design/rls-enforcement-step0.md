# Track 1 — RLS enforcement (decision_records + HITL): step-0 state + plan

DB backstop for the two tenant-isolation bugs dogfooding found in `hitl_routes.py`
(`list_requests` filtered `tenant_id=NULL`; `verify_request` cross-tenant IDOR). Grounds
in the #12 `memories` pattern and adds the FORCE + non-superuser runtime role #12 deferred.

## Step 0 — current RLS state (live prod, 2026-08-07, via public proxy — read-only)

**Runtime role.** The app connects as **`postgres`** — `rolsuper=t`, `rolbypassrls=t`, and
the **owner** of every table. A superuser/BYPASSRLS role bypasses ALL row-level security, so
**RLS is currently inert regardless of any policy.** `grafomem_rt` **does not exist**.

**Per-table:**

| table | RLS enabled | FORCE | policy | owner |
|---|---|---|---|---|
| `memories` | ✅ | ❌ | `tenant_isolation_memories` | postgres |
| `memory_embeddings` | ✅ | ❌ | `tenant_isolation_embeddings` | postgres |
| `decision_records` | ❌ | ❌ | none | postgres |
| `hitl_approval_requests` | ❌ | ❌ | none | postgres |
| `hitl_approvers` | ❌ | ❌ | none | postgres |

So even the #12 `memories` work is inert in prod (bypassed by the postgres connection), and
the three target tables have **no RLS at all**. All three have a `tenant_id` column (verified).

**Code half — `app.current_tenant` coverage (review-hunt #1).** Every path touching the
target tables and whether it sets the context today:

| file | table touches | sets `app.current_tenant`? |
|---|---|---|
| `cloud/decision_trail.py` | 7 (decision_records) | ❌ **no** |
| `cloud/hitl_routes.py` | 11 (HITL) | ❌ **no** |
| `cloud/erasure_proof.py` | 1 (decision_records scrub) | ❌ **no** |
| `cloud/orchestrator.py` | 1 (INSERT hitl_approval_requests) | ❌ **no** |
| `cloud/push_service.py` | 2 (hitl_approvers scan) | ❌ **no** |

**0 of 5** set it. Only the GMP `memories` backend (`backends/postgres_gmp.py:231`) sets
`set_config('app.current_tenant', …, true)` today. Under the postgres connection this doesn't
matter; the moment we repoint to `grafomem_rt`, any unset path returns 0 rows / fails WITH CHECK.

## The two review hunts, addressed

1. **`app.current_tenant` on 100% of runtime connections.** The code half must set it in all
   5 files above. Pattern: `SET LOCAL app.current_tenant = <tenant>` (via `set_config(…, true)`)
   at the start of each transaction, using the `tenant_id` already in scope. Cross-tenant /
   background paths (`push_service` scans approvers across tenants; `erasure_proof`; any Merkle/
   anchor job) use the `'admin'` sentinel the policy allows. The PR will include a test that
   FAILS if a decision_records/HITL query runs without the context set (proving coverage).
2. **`grafomem_rt` non-superuser so FORCE applies.** `ops/rls_decision_hitl.sql` creates it
   `NOSUPERUSER NOBYPASSRLS` (non-owner) and idempotently re-asserts those attributes. FORCE is
   added on all five tables. The switch is repointing the app's DB user to it (Phase C).

## Rollout phasing (each step reversible; enforcement only activates at C)

- **A. Code half** — set `app.current_tenant` everywhere (the diff). Deploy. App still connects
  as postgres ⇒ context is set but RLS inert ⇒ **zero behavior change** (safe to ship normally).
- **B. `ops/rls_decision_hitl.sql`** — role + grants + ENABLE/FORCE + policies. Still inert under
  postgres ⇒ **zero behavior change**. Manual apply as owner, post-review.
- **C. Repoint** `GRAFOMEM_DB_URL` user → `grafomem_rt` (Railway env, backend + daemon). This
  **activates** enforcement. Rollback = repoint to postgres (instant, no data change).

**Do NOT do C before A is deployed** — any uncovered connection breaks. B before A is harmless.

## Open design choices flagged for review

- **WITH CHECK added** (the SQL blocks cross-tenant INSERT/UPDATE); #12's `memories` policy is
  USING-only. Stronger, but if a legitimate path writes another tenant's row under a non-admin
  context it would now fail — the coverage audit says none do, but calling it out.
- **Whole-app repoint** means `grafomem_rt` needs DML on *every* table (granted schema-wide +
  default privileges), not just the three. Alternative: keep a privileged pool for non-RLS
  tables — rejected as more error-prone than one restricted role + schema-wide grants.
- **`memories`/`memory_embeddings` brought to FORCE** for uniformity per the FORCE ask; they were
  ENABLE-only under #12.

## Deliverables in this PR
- `ops/rls_decision_hitl.sql` — Phase A/B migration (this).
- (next commit) the code-half diff across the 5 files + a coverage-enforcing test.
- Manual apply + Phase-C repoint happen only after review.
