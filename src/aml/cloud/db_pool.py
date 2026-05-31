"""
GRAFOMEM Database Pool — centralized psycopg connection pooling.

Replaces the per-service lazy ``psycopg.connect()`` pattern with a shared
``psycopg_pool.ConnectionPool``.  All cloud services accept an optional
``pool`` parameter; when provided they checkout/return connections instead
of holding a persistent one.

Usage::

    pool = DatabasePool(db_url, min_size=5, max_size=20)
    pool.open()
    # ... pass pool to services ...
    pool.close()

Environment Variables
---------------------
GRAFOMEM_DB_POOL_MIN : int
    Minimum connections to keep open (default 5).
GRAFOMEM_DB_POOL_MAX : int
    Maximum connections (default 20).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.db_pool")


class DatabasePool:
    """Centralized connection pool wrapping ``psycopg_pool.ConnectionPool``.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    min_size : int
        Minimum connections to keep open.
    max_size : int
        Maximum connections allowed.
    """

    def __init__(
        self,
        db_url: str,
        min_size: int | None = None,
        max_size: int | None = None,
    ) -> None:
        self._db_url = db_url
        self._min_size = min_size or int(os.environ.get("GRAFOMEM_DB_POOL_MIN", "5"))
        self._max_size = max_size or int(os.environ.get("GRAFOMEM_DB_POOL_MAX", "20"))
        self._pool = None

    def open(self) -> None:
        """Open the connection pool.  Safe to call multiple times."""
        if self._pool is not None:
            return

        try:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                self._db_url,
                min_size=self._min_size,
                max_size=self._max_size,
                kwargs={"row_factory": dict_row, "autocommit": True},
            )
            logger.info(
                "Database pool opened (min=%d, max=%d)",
                self._min_size, self._max_size,
            )
        except ImportError:
            logger.warning(
                "psycopg_pool not installed — falling back to direct connections"
            )
        except Exception as e:
            logger.warning("Failed to create connection pool: %s", e)

    def close(self) -> None:
        """Close all pooled connections."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None
            logger.info("Database pool closed")

    @contextmanager
    def connection(self):
        """Checkout a connection from the pool, return it on exit.

        Falls back to a direct connection if the pool is unavailable.

        Yields
        ------
        psycopg.Connection
            A dict-row connection with autocommit enabled.
        """
        if self._pool is not None:
            with self._pool.connection() as conn:
                yield conn
        else:
            conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
            try:
                yield conn
            finally:
                conn.close()

    def getconn(self) -> psycopg.Connection[dict[str, Any]]:
        """Get a connection (compatible with legacy _get_conn pattern).

        When the pool is active, returns a connection checked out from the
        pool.  The caller is responsible for returning it (via ``putconn``).
        When the pool is unavailable, creates a direct connection.

        Returns
        -------
        psycopg.Connection
            A dict-row connection with autocommit enabled.
        """
        if self._pool is not None:
            return self._pool.getconn()
        return psycopg.connect(
            self._db_url, row_factory=dict_row, autocommit=True,
        )

    def putconn(self, conn: psycopg.Connection) -> None:
        """Return a connection to the pool.

        If the pool is unavailable, closes the connection directly.
        """
        if self._pool is not None:
            self._pool.putconn(conn)
        else:
            conn.close()

    @property
    def stats(self) -> dict[str, Any]:
        """Pool statistics (empty dict if no pool)."""
        if self._pool is None:
            return {"pooled": False}
        s = self._pool.get_stats()
        return {
            "pooled": True,
            "pool_min": self._min_size,
            "pool_max": self._max_size,
            "pool_size": s.get("pool_size", 0),
            "pool_available": s.get("pool_available", 0),
            "requests_waiting": s.get("requests_waiting", 0),
        }

    @property
    def is_active(self) -> bool:
        """Whether the pool is open and active."""
        return self._pool is not None


class RoutingPool:
    """Connection pool with optional read-replica routing.

    Wraps a primary DatabasePool and an optional read-replica pool.
    When ``GRAFOMEM_DB_READ_URL`` is set, read-only queries are routed
    to the replica.  Falls back to primary if replica is unavailable.

    The routing is transparent to services — they call ``getconn()``
    as before and get routed automatically.  Services that need
    explicit read routing can call ``getconn(readonly=True)``.

    Environment Variables
    ---------------------
    GRAFOMEM_DB_READ_URL : str
        PostgreSQL connection URI for the read replica.
        If not set, all connections go to the primary.
    GRAFOMEM_DB_READ_POOL_MIN : int
        Minimum read replica connections (default 3).
    GRAFOMEM_DB_READ_POOL_MAX : int
        Maximum read replica connections (default 10).

    Usage::

        pool = RoutingPool(primary_url)
        pool.open()
        conn = pool.getconn()                # primary
        conn = pool.getconn(readonly=True)    # replica if available
        pool.close()
    """

    def __init__(
        self,
        primary_url: str,
        *,
        read_url: str | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        read_min_size: int | None = None,
        read_max_size: int | None = None,
    ) -> None:
        self._primary = DatabasePool(
            primary_url,
            min_size=min_size,
            max_size=max_size,
        )

        read_url = read_url or os.environ.get("GRAFOMEM_DB_READ_URL")
        self._replica: DatabasePool | None = None
        if read_url:
            self._replica = DatabasePool(
                read_url,
                min_size=read_min_size or int(os.environ.get("GRAFOMEM_DB_READ_POOL_MIN", "3")),
                max_size=read_max_size or int(os.environ.get("GRAFOMEM_DB_READ_POOL_MAX", "10")),
            )
            logger.info("Read replica pool configured")

    def open(self) -> None:
        """Open primary and replica pools."""
        self._primary.open()
        if self._replica:
            try:
                self._replica.open()
                logger.info("Read replica pool opened")
            except Exception as e:
                logger.warning(
                    "Read replica pool failed to open: %s (falling back to primary)", e,
                )
                self._replica = None

    def close(self) -> None:
        """Close all pools."""
        self._primary.close()
        if self._replica:
            self._replica.close()

    def getconn(self, *, readonly: bool = False) -> psycopg.Connection[dict[str, Any]]:
        """Get a connection, optionally from the read replica.

        Parameters
        ----------
        readonly : bool
            If True and a read replica is available, returns a
            connection from the replica pool.  Falls back to primary
            if replica is unavailable.
        """
        if readonly and self._replica and self._replica.is_active:
            try:
                return self._replica.getconn()
            except Exception as e:
                logger.warning("Read replica getconn failed, falling back: %s", e)
        return self._primary.getconn()

    def putconn(self, conn: psycopg.Connection) -> None:
        """Return a connection to the appropriate pool."""
        if self._replica and self._replica.is_active:
            try:
                self._replica.putconn(conn)
                return
            except Exception:
                pass
        self._primary.putconn(conn)

    @contextmanager
    def connection(self, *, readonly: bool = False):
        """Context manager for connection checkout/return."""
        conn = self.getconn(readonly=readonly)
        try:
            yield conn
        finally:
            self.putconn(conn)

    @property
    def stats(self) -> dict[str, Any]:
        """Pool statistics for primary and replica."""
        result: dict[str, Any] = {"primary": self._primary.stats}
        if self._replica:
            result["replica"] = self._replica.stats
        else:
            result["replica"] = {"configured": False}
        return result

    @property
    def is_active(self) -> bool:
        """Whether the primary pool is active."""
        return self._primary.is_active

    @property
    def has_replica(self) -> bool:
        """Whether a read replica is configured and active."""
        return self._replica is not None and self._replica.is_active
