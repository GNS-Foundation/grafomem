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
                
                # Run the retention sweep (excluding gcrumbs_breadcrumbs as it is an append-only ledger)
                self._apply_retention_policy(conn, "decision_records", "created_at")
        except Exception as e:
            logger.error("SIEM export sweep failed: %s", e, exc_info=True)

    def _export_table(self, conn, table_name: str):
        """Export new records for a specific table."""
        with conn.cursor() as cur:
            # Get cursor
            cur.execute("SELECT last_exported_time, last_exported_ref FROM siem_export_cursors WHERE table_name = %s", (table_name,))
            row = cur.fetchone()
            if not row:
                logger.warning(f"No SIEM cursor found for {table_name}")
                return
            last_time, last_ref = row

            # Fetch batch
            if table_name == "decision_records":
                # decision_records.created_at is TIMESTAMPTZ, primary key is decision_id
                cur.execute(f"""
                    SELECT decision_id as id, tenant_id, store_id, session_id, created_at as timestamp, 
                           query, retrieved_refs, model_id, raw_output 
                    FROM {table_name} 
                    WHERE (created_at, decision_id) > (%s, %s)
                    ORDER BY created_at ASC, decision_id ASC LIMIT %s
                """, (last_time, last_ref, self.batch_size))
            else:
                # gcrumbs_breadcrumbs.created_at is DOUBLE PRECISION, primary key is breadcrumb_id
                # Convert last_time back to float for comparison if needed, or cast created_at to timestamp
                cur.execute(f"""
                    SELECT breadcrumb_id as id, tenant_id, seq, event_type, payload, payload_hash, 
                           payload_canon, prev_id, signature, signer_pubkey, source_type, source_ref, 
                           to_timestamp(created_at) as timestamp 
                    FROM {table_name} 
                    WHERE (to_timestamp(created_at), breadcrumb_id) > (%s, %s)
                    ORDER BY created_at ASC, breadcrumb_id ASC LIMIT %s
                """, (last_time, last_ref, self.batch_size))
            
            records = cur.fetchall()
            if not records:
                return

            # Format for SIEM
            columns = [desc[0] for desc in cur.description]
            payload = []
            max_time = last_time
            max_ref = last_ref
            
            for r in records:
                record_dict = dict(zip(columns, r))
                # Convert datetimes, jsonb, bytes for JSON serialization
                for k, v in record_dict.items():
                    if isinstance(v, datetime):
                        record_dict[k] = v.isoformat()
                    elif isinstance(v, bytes) or isinstance(v, memoryview):
                        record_dict[k] = bytes(v).hex()
                
                payload.append({
                    "event_type": table_name,
                    "data": record_dict
                })
                # Cursor tracks the highest tuple (timestamp, id)
                rec_time = r[columns.index('timestamp')]
                rec_id = r[columns.index('id')]
                
                if rec_time > max_time or (rec_time == max_time and rec_id > max_ref):
                    max_time = rec_time
                    max_ref = rec_id

            # Send to SIEM
            try:
                response = httpx.post(self.webhook_url, json={"events": payload}, timeout=10.0)
                response.raise_for_status()
                
                # Update cursor
                cur.execute(
                    "UPDATE siem_export_cursors SET last_exported_time = %s, last_exported_ref = %s, updated_at = %s WHERE table_name = %s",
                    (max_time, max_ref, datetime.now(timezone.utc), table_name)
                )
                conn.commit()
                logger.info(f"Successfully exported {len(records)} records from {table_name} to SIEM. New cursor: {max_time} | {max_ref}")
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
            cur.execute("SELECT last_exported_time, last_exported_ref FROM siem_export_cursors WHERE table_name = %s", (table_name,))
            row = cur.fetchone()
            if not row or row[0] is None or row[0] == datetime(1970, 1, 1, tzinfo=timezone.utc):
                logger.debug(f"Retention skipped: No valid export cursor found for {table_name}")
                return
            last_exported_time, last_exported_ref = row

            # Delete old records that have safely been exported
            # We can only delete records that are older than both cutoff_date AND last_exported_time.
            # We strictly enforce that time_col < last_exported_time or (time_col = last_exported_time and id <= last_exported_ref)
            
            id_col = "decision_id" if table_name == "decision_records" else "breadcrumb_id"
            
            # Since gcrumbs_breadcrumbs is excluded from retention we don't need to cast double precision here, 
            # but we keep it generic in case it's added back.
            
            cur.execute(f"""
                DELETE FROM {table_name} 
                WHERE {time_col} < %s 
                AND ({time_col}, {id_col}) <= (%s, %s)
            """, (cutoff_date, last_exported_time, last_exported_ref))
            
            deleted_count = cur.rowcount
            if deleted_count > 0:
                conn.commit()
                logger.info(f"Pruned {deleted_count} records from {table_name} older than {self.retention_days} days.")
