import os
import logging
import json
import httpx
from datetime import datetime, timezone, timedelta
from psycopg_pool import ConnectionPool

logger = logging.getLogger("grafomem.cloud.siem_exporter")

class SiemExporter:
    """Background daemon that exports audit logs to a SIEM and applies retention policies."""

    def __init__(self, db_url: str):
        self.db_url = db_url
        self.webhook_url = os.environ.get("SIEM_WEBHOOK_URL")
        # Default 180 days retention as requested by enterprise compliance
        self.retention_days = int(os.environ.get("LOG_RETENTION_DAYS", "180"))
        self.batch_size = int(os.environ.get("SIEM_BATCH_SIZE", "100"))

    def run_sweep(self):
        """Main entrypoint called by the APScheduler."""
        if not self.webhook_url:
            logger.debug("SIEM_WEBHOOK_URL not configured. Skipping SIEM export.")
            return

        logger.info("Starting SIEM export and retention sweep")
        import psycopg
        try:
            with psycopg.connect(self.db_url) as conn:
                self._export_table(conn, "decision_records")
                self._export_table(conn, "gcrumbs_breadcrumbs")
                
                # Run the retention sweep
                self._apply_retention_policy(conn, "decision_records", "timestamp")
                self._apply_retention_policy(conn, "gcrumbs_breadcrumbs", "created_at")
        except Exception as e:
            logger.error("SIEM export sweep failed: %s", e, exc_info=True)

    def _export_table(self, conn, table_name: str):
        """Export new records for a specific table."""
        with conn.cursor() as cur:
            # Get cursor
            cur.execute("SELECT last_exported_id FROM siem_export_cursors WHERE table_name = %s", (table_name,))
            row = cur.fetchone()
            if not row:
                logger.warning(f"No SIEM cursor found for {table_name}")
                return
            last_id = row[0]

            # Fetch batch
            if table_name == "decision_records":
                cur.execute(f"SELECT id, tenant_id, timestamp, decision, rationale, context_snapshot, policy_version FROM {table_name} WHERE id > %s ORDER BY id ASC LIMIT %s", (last_id, self.batch_size))
            else:
                cur.execute(f"SELECT id, tenant_id, crumb_hash, prev_hash, epoch_hash, record_type, record_id, created_at FROM {table_name} WHERE id > %s ORDER BY id ASC LIMIT %s", (last_id, self.batch_size))
            
            records = cur.fetchall()
            if not records:
                return

            # Format for SIEM
            columns = [desc[0] for desc in cur.description]
            payload = []
            max_id = last_id
            for r in records:
                record_dict = dict(zip(columns, r))
                # Convert datetimes to isoformat for JSON serialization
                for k, v in record_dict.items():
                    if isinstance(v, datetime):
                        record_dict[k] = v.isoformat()
                
                payload.append({
                    "event_type": table_name,
                    "data": record_dict
                })
                max_id = max(max_id, record_dict["id"])

            # Send to SIEM
            try:
                response = httpx.post(self.webhook_url, json={"events": payload}, timeout=10.0)
                response.raise_for_status()
                
                # Update cursor
                cur.execute(
                    "UPDATE siem_export_cursors SET last_exported_id = %s, last_exported_at = %s WHERE table_name = %s",
                    (max_id, datetime.now(timezone.utc), table_name)
                )
                conn.commit()
                logger.info(f"Successfully exported {len(records)} records from {table_name} to SIEM. New cursor: {max_id}")
            except httpx.RequestError as e:
                logger.error(f"Failed to send logs to SIEM webhook: {e}")
                conn.rollback()
            except httpx.HTTPStatusError as e:
                logger.error(f"SIEM webhook returned HTTP {e.response.status_code}: {e.response.text}")
                conn.rollback()

    def _apply_retention_policy(self, conn, table_name: str, time_col: str):
        """Delete logs older than retention_days, but ONLY if they have been exported."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        
        with conn.cursor() as cur:
            # Get safe cursor limit
            cur.execute("SELECT last_exported_id FROM siem_export_cursors WHERE table_name = %s", (table_name,))
            row = cur.fetchone()
            if not row:
                return
            last_exported_id = row[0]

            # Delete old records that have safely been exported
            cur.execute(f"""
                DELETE FROM {table_name} 
                WHERE {time_col} < %s 
                AND id <= %s
            """, (cutoff_date, last_exported_id))
            
            deleted_count = cur.rowcount
            if deleted_count > 0:
                logger.info(f"Retention policy applied: Deleted {deleted_count} old records from {table_name}")
            conn.commit()
