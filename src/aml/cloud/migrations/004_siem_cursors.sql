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
