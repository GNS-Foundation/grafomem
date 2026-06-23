"""
GRAFOMEM Erasure Sweeper — Asynchronous bounded-window cleanup for right-to-be-forgotten.

When a primary memory is deleted, its embedding is marked with `erasure_pending`
instead of being cascaded synchronously. This sweeper periodically cleans up
those orphaned embeddings, guaranteeing erasure within a documented window
(e.g., 5 minutes) while allowing the embedding to outlive the primary momentarily
for cryptographic verification of the deletion coverage.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.erasure_sweeper")


class ErasureSweeper:
    """Async/Periodic job to hard-delete embeddings marked for erasure.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    window_minutes : int
        The bounded SLA for right-to-be-forgotten sweeps. Embeddings
        pending erasure older than this window are swept.
    """

    def __init__(self, db_url: str, window_minutes: int = 5, table_prefix: str = "") -> None:
        self._db_url = db_url
        self._window = timedelta(minutes=window_minutes)
        # Allows targeting demo schemas (e.g., 'demo_')
        self._table_prefix = table_prefix

    def sweep(self) -> int:
        """Find and hard-delete pending embeddings outside the safety window.

        Returns the number of embeddings swept.
        """
        table_name = f"{self._table_prefix}memory_embeddings"
        cutoff = datetime.now(timezone.utc) - self._window

        logger.info(f"Sweeping {table_name} for embeddings marked before {cutoff}")

        # Note: In a true production sweep, we might want to do this in batches
        # and log specific tenant/fact_refs swept to update coverage records.
        # For the demo, we just sweep all eligible and return the count.
        with psycopg.connect(self._db_url, row_factory=dict_row, autocommit=True) as conn:
            # We want to fetch the refs before deleting so we can log them
            with conn.transaction():
                cur = conn.execute(
                    f"SELECT ref, tenant_id FROM {table_name} "
                    "WHERE erasure_pending IS NOT NULL AND erasure_pending <= %s",
                    (cutoff,)
                )
                rows = cur.fetchall()

                if not rows:
                    return 0

                refs = [r["ref"] for r in rows]
                
                # Hard delete
                conn.execute(
                    f"DELETE FROM {table_name} WHERE ref = ANY(%s)",
                    (refs,)
                )

                for r in rows:
                    logger.info("Swept orphaned embedding for tenant=%s ref=%s", r["tenant_id"], r["ref"])

                return len(rows)

