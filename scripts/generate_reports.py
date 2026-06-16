#!/usr/bin/env python3
import json
import os
import psycopg
from datetime import datetime, timezone
from psycopg.rows import dict_row

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from aml.cloud.regulatory import RegulatoryReportService, ReportType

DB_URL = os.environ.get("DATABASE_URL", "dbname=grafomem")

def main():
    print(f"Connecting to {DB_URL}...")
    conn = psycopg.connect(DB_URL, row_factory=dict_row)
    
    # Get the first tenant (typically the test tenant)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tenants LIMIT 1")
        row = cur.fetchone()
        if not row:
            print("No tenants found in tenants!")
            return
        tenant_id = row["id"]
        
    print(f"Generating full audit report for tenant {tenant_id}...")
    
    # We pass None for other services; the RegulatoryReportService connects to DB directly
    # and reads the views/tables directly for its stats.
    svc = RegulatoryReportService(db_url=DB_URL)
    svc.ensure_schema()
    
    report = svc.generate(tenant_id, ReportType.FULL_AUDIT, period_days=30)
    
    results_dir = os.path.join(os.path.dirname(__file__), '..', 'results')
    os.makedirs(results_dir, exist_ok=True)
    report_path = os.path.join(results_dir, "full_audit_report.json")
    
    # Convert report to dict, handling datetime objects
    def json_serial(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")
        
    import dataclasses
    with open(report_path, "w") as f:
        json.dump(dataclasses.asdict(report), f, default=json_serial, indent=2)
        
    print(f"Successfully generated report. Saved to {report_path}")
    print(f"Overall finding: {report.content.get('overall_finding')}")

if __name__ == "__main__":
    main()
