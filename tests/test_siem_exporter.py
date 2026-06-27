import pytest
import psycopg
import httpx
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from aml.cloud.siem_exporter import SiemExporter

@pytest.fixture
def db_url():
    return os.environ.get(
        "GRAFOMEM_DB_URL",
        "postgresql://grafomem:dev@localhost:5432/grafomem"
    )

@pytest.fixture
def clean_db(db_url):
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # Emulate 004_siem_cursors.sql
            cur.execute("DROP TABLE IF EXISTS siem_export_cursors CASCADE")
            cur.execute("""
                CREATE TABLE siem_export_cursors (
                    table_name VARCHAR(255) PRIMARY KEY,
                    last_exported_time TIMESTAMPTZ DEFAULT '1970-01-01 00:00:00+00',
                    last_exported_ref TEXT DEFAULT '',
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            cur.execute("TRUNCATE TABLE decision_records CASCADE")
            cur.execute("TRUNCATE TABLE siem_export_cursors CASCADE")
            
            cur.execute("INSERT INTO siem_export_cursors (table_name, last_exported_time, last_exported_ref) VALUES ('decision_records', '1970-01-01 00:00:00+00', '')")
            conn.commit()
    return db_url

def test_at_least_once_idempotent_delivery(clean_db, monkeypatch):
    monkeypatch.setenv("SIEM_WEBHOOK_URL", "http://mock-siem.local")
    monkeypatch.setenv("SIEM_BATCH_SIZE", "5")
    
    # Insert 5 records
    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            for i in range(1, 6):
                cur.execute("""
                    INSERT INTO decision_records (decision_id, tenant_id, store_id, created_at, query, retrieved_refs, model_id, raw_output)
                    VALUES (%s, 't1', 's1', now(), 'query', '[]', 'm1', 'output')
                """, (f"dec_{i}",))
        conn.commit()

    exporter = SiemExporter(clean_db)
    
    # Mock httpx.post to fail on the first call, succeed on the second
    call_count = 0
    received_payloads = []

    class MockResponse:
        def raise_for_status(self):
            pass

    def mock_post(url, json, timeout):
        nonlocal call_count
        call_count += 1
        received_payloads.append(json)
        if call_count == 1:
            raise httpx.RequestError("Simulated mid-batch crash")
        return MockResponse()

    with patch('httpx.post', side_effect=mock_post):
        # First run: will fail and catch the exception inside run_sweep
        exporter.run_sweep()
        
        # Cursor should still be '1970-01-01 00:00:00+00'
        with psycopg.connect(clean_db) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT last_exported_ref FROM siem_export_cursors WHERE table_name = 'decision_records'")
                cursor_ref = cur.fetchone()[0]
                assert cursor_ref == ''

        # Second run: will succeed
        exporter.run_sweep()

        # Cursor should now be 'dec_5'
        with psycopg.connect(clean_db) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT last_exported_ref FROM siem_export_cursors WHERE table_name = 'decision_records'")
                cursor_ref = cur.fetchone()[0]
                assert cursor_ref == 'dec_5'

    assert call_count == 2
    # The first failed payload and second successful payload should contain the EXACT SAME 5 records
    # proving at-least-once delivery requiring deduplication by ID on the SIEM side.
    assert len(received_payloads) == 2
    assert len(received_payloads[0]["events"]) == 5
    assert len(received_payloads[1]["events"]) == 5
    assert received_payloads[0]["events"][0]["data"]["id"] == 'dec_1'
    assert received_payloads[1]["events"][0]["data"]["id"] == 'dec_1'

def test_retention_pruning_safety(clean_db, monkeypatch):
    monkeypatch.setenv("SIEM_WEBHOOK_URL", "http://mock-siem.local")
    monkeypatch.setenv("LOG_RETENTION_DAYS", "180")
    
    # Insert old records (older than 180 days)
    old_date = datetime.now(timezone.utc) - timedelta(days=200)
    
    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            for i in range(1, 6):
                cur.execute("""
                    INSERT INTO decision_records (decision_id, tenant_id, store_id, created_at, query, retrieved_refs, model_id, raw_output)
                    VALUES (%s, 't1', 's1', %s, 'query', '[]', 'm1', 'output')
                """, (f"dec_{i}", old_date + timedelta(seconds=i)))
        conn.commit()

    exporter = SiemExporter(clean_db)
    
    # Empty cursor case: SET cursor to NULL explicitly
    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE siem_export_cursors SET last_exported_time = NULL WHERE table_name = 'decision_records'")
        conn.commit()
        
        # Apply retention directly (bypassing export to isolate test)
        exporter._apply_retention_policy(conn, "decision_records", "created_at")
        
        # Verify 0 deleted
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM decision_records")
            assert cur.fetchone()[0] == 5

    # Boundary case: SET cursor to dec_3's time and ref
    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            # We must use exactly the old_date + 3 seconds
            cursor_time = old_date + timedelta(seconds=3)
            cur.execute("UPDATE siem_export_cursors SET last_exported_time = %s, last_exported_ref = %s WHERE table_name = 'decision_records'", (cursor_time, 'dec_3'))
        conn.commit()
        
        exporter._apply_retention_policy(conn, "decision_records", "created_at")
        
        # Records 1, 2, 3 should be deleted, leaving 4 and 5
        with conn.cursor() as cur:
            cur.execute("SELECT decision_id FROM decision_records ORDER BY decision_id ASC")
            remaining = [r[0] for r in cur.fetchall()]
            assert remaining == ['dec_4', 'dec_5']

def test_tie_breaker_pruning_safety(clean_db, monkeypatch):
    monkeypatch.setenv("SIEM_WEBHOOK_URL", "http://mock-siem.local")
    monkeypatch.setenv("LOG_RETENTION_DAYS", "180")
    
    # Insert two old records with EXACTLY the same timestamp
    old_date = datetime.now(timezone.utc) - timedelta(days=200)
    
    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            # We insert dec_A and dec_B with the exact same timestamp
            cur.execute("""
                INSERT INTO decision_records (decision_id, tenant_id, store_id, created_at, query, retrieved_refs, model_id, raw_output)
                VALUES (%s, 't1', 's1', %s, 'query', '[]', 'm1', 'output')
            """, ("dec_A", old_date))
            cur.execute("""
                INSERT INTO decision_records (decision_id, tenant_id, store_id, created_at, query, retrieved_refs, model_id, raw_output)
                VALUES (%s, 't1', 's1', %s, 'query', '[]', 'm1', 'output')
            """, ("dec_B", old_date))
        conn.commit()

    exporter = SiemExporter(clean_db)
    
    # Boundary case: SET cursor exactly to the shared time, but ref = 'dec_A'
    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE siem_export_cursors SET last_exported_time = %s, last_exported_ref = %s WHERE table_name = 'decision_records'", (old_date, 'dec_A'))
        conn.commit()
        
        # Apply retention
        exporter._apply_retention_policy(conn, "decision_records", "created_at")
        
        # 'dec_A' should be pruned because (time, 'dec_A') <= (time, 'dec_A')
        # 'dec_B' should NOT be pruned because (time, 'dec_B') > (time, 'dec_A')
        with conn.cursor() as cur:
            cur.execute("SELECT decision_id FROM decision_records ORDER BY decision_id ASC")
            remaining = [r[0] for r in cur.fetchall()]
            assert remaining == ['dec_B']
