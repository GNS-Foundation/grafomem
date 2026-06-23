-- SIEM Export cursors table for high-water mark tracking
CREATE TABLE IF NOT EXISTS siem_export_cursors (
    id SERIAL PRIMARY KEY,
    table_name VARCHAR(255) NOT NULL UNIQUE,
    last_exported_id BIGINT DEFAULT 0,
    last_exported_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Initialize the cursors for the two audit tables
INSERT INTO siem_export_cursors (table_name, last_exported_id) 
VALUES ('decision_records', 0)
ON CONFLICT (table_name) DO NOTHING;

INSERT INTO siem_export_cursors (table_name, last_exported_id) 
VALUES ('gcrumbs_breadcrumbs', 0)
ON CONFLICT (table_name) DO NOTHING;
