# Schema migrations

Applied as a **release step** by the migrate role — see `../migrations_runner.py`. In cloud
the runtime process does **no** DDL; run migrations out of band:

```
GRAFOMEM_MIGRATE_URL=postgresql://grafomem_migrate:...@host/db \
GRAFOMEM_RUNTIME_ROLE=grafomem_rt \
    python -m aml.cloud.migrations_runner                 # apply pending
... python -m aml.cloud.migrations_runner --baseline 005_hitl_approval.sql   # record, no re-run
```

The `.sql` files are shipped in the wheel via `[tool.setuptools.package-data]` in `pyproject.toml`
(`cloud/migrations/*.sql`) — deployed containers run the installed wheel, not the source tree, so a
file that isn't declared there is silently absent.

## Immutability

Migrations are **append-only and immutable** from the first migration merged after the release runner
landed. Once a numbered `.sql` has been applied in any environment we control (staging or prod), it is
**never edited** — fixes ship as a new, higher-numbered, idempotent migration.

**One-time exception:** `006_push_tokens.sql` was amended in place on 2026-09-15 because it had never
been applied in any environment we control. `008_push_tokens_converge.sql` is the idempotent convergence
guard for any external self-hoster who applied the original 006 (adds `tenant_id` / the approver FK if
missing). From 007/008 onward, immutability holds.

## No new tables via `ensure_schema`

From 2026-09-15 on, **new tables are numbered migrations, never `ensure_schema` DDL.** A service's
`ensure_schema` must not introduce a new table. The existing `ensure_schema` DDL is being retired: in
cloud the runtime process does no boot DDL (it runs as `grafomem_rt`, which owns nothing), so
`ensure_schema` is gated off there — `ErasureLedger` first, the rest once the `--ensure-schema` release
step lands. When an existing `ensure_schema` table next needs a change, convert it to a numbered migration
and record the current state with a **verify-first baseline**.

## Grant rule

In a **split-role** deployment (`GRAFOMEM_RUNTIME_ROLE` set) every migration that `CREATE`s a table MUST,
in the same file, `GRANT` the runtime role its DML — and `grafomem_ledger` for ledger tables — or the
runtime process (which owns no tables) cannot use the new table. The runner **refuses** a `CREATE TABLE`
without the grant. Wrap the `GRANT` in a `DO $$ … IF EXISTS (SELECT 1 FROM pg_roles …) … $$` guard so it
is a no-op in single-role self-host. `ALTER DEFAULT PRIVILEGES … TO grafomem_rt` (operator runbook) is
the backstop; the in-file grant is the explicit belt.

### Ledger-class tables

A migration declares a table **append-only ledger-class** with a header marker — **`-- class: ledger`**
on its own line — never inferred from the name. For a ledger-class migration the runner requires, per
CREATE'd table:

- the **ledger role** (`grafomem_ledger`) granted **INSERT + SELECT** (it appends, and reads back for
  restore-scrub), and **not** UPDATE/DELETE (append-only);
- the **runtime role** (`grafomem_rt`) **SELECT-only**, which needs an **explicit
  `REVOKE INSERT, UPDATE, DELETE ON <table> FROM grafomem_rt;`** in the same file — because
  `ALTER DEFAULT PRIVILEGES` grants the runtime role full DML on every migrate-created table, so the
  revoke is what actually makes it read-only.

The runner rejects a ledger-class migration that violates any of these. `009_erasure_ledger.sql` carries
the marker for classification but predates the rule and is applied in every environment we control, so it
is grandfathered (exempt from the REVOKE requirement); new ledger-class migrations are not.
