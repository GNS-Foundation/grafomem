import json
from typing import Optional

class ErasureLedger:
    """
    Append-only erasure ledger in the external key store.
    Records tenant-level crypto-erasures and subject-level Article 17 erasures.
    Authoritative log used to re-scrub revived data on backup restore.
    """
    def __init__(self, key_store_url: str, open: bool = True):
        self._key_store_url = key_store_url
        
        try:
            import psycopg
            from psycopg_pool import ConnectionPool
        except ImportError as e:
            raise RuntimeError("ErasureLedger requires psycopg and psycopg_pool") from e

        self._pool = ConnectionPool(self._key_store_url, min_size=1, max_size=10, open=open)

    def ensure_schema(self):
        with self._pool.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS erasure_ledger (
                    entry_id        TEXT PRIMARY KEY,
                    tenant_id       TEXT NOT NULL,
                    entry_type      TEXT NOT NULL,
                    fact_ref        INTEGER,
                    content_hash    TEXT,
                    certificate     JSONB,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)

    def record_subject_erasure(self, entry_id: str, tenant_id: str, fact_ref: int, content_hash: Optional[str], certificate: dict) -> str:
        """Append a subject erasure event."""
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO erasure_ledger 
                   (entry_id, tenant_id, entry_type, fact_ref, content_hash, certificate) 
                   VALUES (%s, %s, 'subject_erasure', %s, %s, %s)""",
                (entry_id, tenant_id, fact_ref, content_hash, json.dumps(certificate))
            )
        return entry_id

    def record_tenant_destruction(self, entry_id: str, tenant_id: str, certificate: dict) -> str:
        """Append a tenant key destruction event."""
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO erasure_ledger 
                   (entry_id, tenant_id, entry_type, certificate) 
                   VALUES (%s, %s, 'tenant_destruction', %s)""",
                (entry_id, tenant_id, json.dumps(certificate))
            )
        return entry_id

    def get_all_erasures(self, tenant_id: Optional[str] = None) -> list[dict]:
        """Fetch all erasure events (for restore-scrub)."""
        query = "SELECT entry_id, tenant_id, entry_type, fact_ref, content_hash, certificate, created_at FROM erasure_ledger"
        params = []
        if tenant_id:
            query += " WHERE tenant_id = %s"
            params.append(tenant_id)
        
        query += " ORDER BY created_at ASC"

        with self._pool.connection() as conn:
            from psycopg.rows import dict_row
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params)
                return cur.fetchall()
