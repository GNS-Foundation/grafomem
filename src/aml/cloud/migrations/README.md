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

## Grant rule

In a **split-role** deployment (`GRAFOMEM_RUNTIME_ROLE` set) every migration that `CREATE`s a table MUST,
in the same file, `GRANT` the runtime role its DML — and `grafomem_ledger` for ledger tables — or the
runtime process (which owns no tables) cannot use the new table. The runner **refuses** a `CREATE TABLE`
without the grant. Wrap the `GRANT` in a `DO $$ … IF EXISTS (SELECT 1 FROM pg_roles …) … $$` guard so it
is a no-op in single-role self-host. `ALTER DEFAULT PRIVILEGES … TO grafomem_rt` (operator runbook) is
the backstop; the in-file grant is the explicit belt.
