-- Cloud Metering Phase 3b — provision the usage reporter's cursor table.
--
-- WHY THIS EXISTS (not self-migrated by the app):
-- The app runs as the least-privilege runtime role (grafomem_rt), which has DML on
-- existing tables but NOT CREATE on schema public. The UsageReporter used to self-create
-- usage_report_cursor via ensure_schema() at first arm; under grafomem_rt that raises
-- "permission denied for schema public", so start() failed and the reporter never ran.
-- The reporter is now tolerant of that (starts on a pre-provisioned table), but the table
-- must be created out-of-band by a superuser — this migration.
--
-- RUN AS: the postgres superuser (Railway Postgres → Query tab, or GRAFOMEM_DB_URL_ROLLBACK).
-- Idempotent. No RLS: the reporter sweeps cross-tenant by explicit tenant_id predicates.
-- Schema is byte-identical to usage_reporter._SCHEMA_SQL so the app's CREATE IF NOT EXISTS
-- no-ops cleanly if it ever runs with DDL rights.

CREATE TABLE IF NOT EXISTS usage_report_cursor (
    tenant_id       TEXT        NOT NULL,
    period_start    TIMESTAMPTZ NOT NULL,
    last_reported   BIGINT      NOT NULL DEFAULT 0,    -- committed high-water-mark
    last_identifier TEXT,                              -- identifier of the last emit
    last_delta      BIGINT      NOT NULL DEFAULT 0,    -- value of the last emit
    last_confirmed  BOOLEAN     NOT NULL DEFAULT TRUE, -- did the last emit confirm?
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, period_start)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON usage_report_cursor TO grafomem_rt;
