-- SIEM Export cursors table for high-water mark tracking
CREATE TABLE IF NOT EXISTS siem_export_cursors (
    table_name VARCHAR(255) PRIMARY KEY,
    last_exported_time TIMESTAMPTZ DEFAULT '1970-01-01 00:00:00+00',
    last_exported_ref TEXT DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- Initialize the cursors for the two audit tables
INSERT INTO siem_export_cursors (table_name, last_exported_time, last_exported_ref) 
VALUES ('decision_records', '1970-01-01 00:00:00+00', '')
ON CONFLICT (table_name) DO NOTHING;

INSERT INTO siem_export_cursors (table_name, last_exported_time, last_exported_ref) 
VALUES ('gcrumbs_breadcrumbs', '1970-01-01 00:00:00+00', '')
ON CONFLICT (table_name) DO NOTHING;

INSERT INTO siem_export_cursors (table_name, last_exported_time, last_exported_ref)
VALUES ('audit_logs', '1970-01-01 00:00:00+00', '')
ON CONFLICT (table_name) DO NOTHING;

-- Runtime role receives DML in split-role deployments: the SIEM exporter, running as the
-- runtime role, updates each table's high-water mark after export. Guarded so it is a no-op
-- in single-role self-host (grafomem_rt absent). Added 2026-09-19 for the split-role rule
-- (this table predates it); already-applied environments skip this file.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafomem_rt') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON siem_export_cursors TO grafomem_rt;
  END IF;
END $$;
