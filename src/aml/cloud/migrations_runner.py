import os
import logging
from pathlib import Path

import psycopg

logger = logging.getLogger("grafomem.migrations")

def apply_migrations(db_url: str) -> None:
    """Run all pending schema migrations from the migrations directory."""
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.exists():
        return

    sql_files = sorted([f for f in migrations_dir.iterdir() if f.name.endswith(".sql")])
    if not sql_files:
        return

    with psycopg.connect(db_url, autocommit=True) as conn:
        # Ensure migrations table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        
        # Get applied migrations
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        applied = {row[0] for row in rows}

        for sql_file in sql_files:
            version = sql_file.name
            if version in applied:
                continue

            logger.info(f"Applying migration: {version}")
            try:
                sql = sql_file.read_text()
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                logger.info(f"Successfully applied {version}")
            except Exception as e:
                logger.error(f"Failed to apply migration {version}: {e}")
                raise
