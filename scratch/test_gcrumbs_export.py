import psycopg
import os
from datetime import datetime, timezone
import httpx
from unittest.mock import patch
from aml.cloud.siem_exporter import SiemExporter

db_url = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")

def check_gcrumbs_query():
    exporter = SiemExporter(db_url)
    
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # Emulate cursor
            cur.execute("TRUNCATE TABLE siem_export_cursors CASCADE")
            cur.execute("INSERT INTO siem_export_cursors (table_name, last_exported_time, last_exported_ref) VALUES ('gcrumbs_breadcrumbs', '1970-01-01 00:00:00+00', '')")
            
            # Create table if not exists (should already exist from integration seams)
            # Actually just insert a fake record to see if the query blows up
            cur.execute("TRUNCATE TABLE gcrumbs_breadcrumbs CASCADE")
            
            cur.execute("""
                INSERT INTO gcrumbs_breadcrumbs (breadcrumb_id, tenant_id, seq, event_type, payload, payload_hash, payload_canon, prev_id, signature, signer_pubkey, source_type, source_ref, created_at)
                VALUES ('bc1', 't1', 1, 'ev', '{}', 'hash', '\\x00', 'prev', 'sig', 'pub', 'src', 'ref', 1600000000.0)
            """)
            conn.commit()
            
    class MockResponse:
        def raise_for_status(self): pass

    def mock_post(*args, **kwargs):
        print("Mock post called!")
        return MockResponse()
        
    with patch('httpx.post', side_effect=mock_post):
        print("Running sweep for gcrumbs_breadcrumbs...")
        exporter.run_sweep()
        print("Sweep completed successfully.")

if __name__ == "__main__":
    check_gcrumbs_query()
