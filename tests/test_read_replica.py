"""GRAFOMEM Read Replica Pool Tests — Sprint 20.

DB-free tests verifying RoutingPool routing logic,
failover behavior, and statistics reporting.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aml.cloud.db_pool import DatabasePool, RoutingPool


class TestDatabasePoolBasics:
    """Verify DatabasePool interface without real DB."""

    def test_init_defaults(self):
        pool = DatabasePool("postgresql://test:test@localhost/test")
        assert pool._db_url == "postgresql://test:test@localhost/test"
        assert pool._pool is None
        assert pool.is_active is False

    def test_stats_no_pool(self):
        pool = DatabasePool("postgresql://test:test@localhost/test")
        assert pool.stats == {"pooled": False}

    def test_close_without_open(self):
        pool = DatabasePool("postgresql://test:test@localhost/test")
        pool.close()  # Should not raise


class TestRoutingPool:
    """Verify RoutingPool routing logic."""

    def test_init_no_replica(self):
        pool = RoutingPool("postgresql://primary/db")
        assert pool._replica is None
        assert pool.has_replica is False

    def test_init_with_replica(self):
        pool = RoutingPool("postgresql://primary/db", read_url="postgresql://replica/db")
        assert pool._replica is not None

    def test_stats_no_replica(self):
        pool = RoutingPool("postgresql://primary/db")
        stats = pool.stats
        assert "primary" in stats
        assert stats["replica"] == {"configured": False}

    def test_stats_with_replica(self):
        pool = RoutingPool("postgresql://primary/db", read_url="postgresql://replica/db")
        stats = pool.stats
        assert "primary" in stats
        assert "replica" in stats
        assert stats["replica"] != {"configured": False}

    def test_getconn_default_uses_primary(self):
        pool = RoutingPool("postgresql://primary/db")
        pool._primary = MagicMock()
        mock_conn = MagicMock()
        pool._primary.getconn.return_value = mock_conn
        conn = pool.getconn()
        pool._primary.getconn.assert_called_once()
        assert conn == mock_conn

    def test_getconn_readonly_uses_replica(self):
        pool = RoutingPool("postgresql://primary/db", read_url="postgresql://replica/db")
        pool._primary = MagicMock()
        pool._replica = MagicMock()
        pool._replica.is_active = True
        mock_conn = MagicMock()
        pool._replica.getconn.return_value = mock_conn
        conn = pool.getconn(readonly=True)
        pool._replica.getconn.assert_called_once()
        pool._primary.getconn.assert_not_called()
        assert conn == mock_conn

    def test_getconn_readonly_falls_back_on_error(self):
        pool = RoutingPool("postgresql://primary/db", read_url="postgresql://replica/db")
        pool._primary = MagicMock()
        pool._replica = MagicMock()
        pool._replica.is_active = True
        pool._replica.getconn.side_effect = Exception("replica down")
        mock_conn = MagicMock()
        pool._primary.getconn.return_value = mock_conn
        conn = pool.getconn(readonly=True)
        assert conn == mock_conn  # Fell back to primary

    def test_getconn_readonly_no_replica_uses_primary(self):
        pool = RoutingPool("postgresql://primary/db")
        pool._primary = MagicMock()
        mock_conn = MagicMock()
        pool._primary.getconn.return_value = mock_conn
        conn = pool.getconn(readonly=True)
        pool._primary.getconn.assert_called_once()
        assert conn == mock_conn

    def test_is_active_delegates_to_primary(self):
        pool = RoutingPool("postgresql://primary/db")
        assert pool.is_active is False

    def test_has_replica_false_when_no_replica(self):
        pool = RoutingPool("postgresql://primary/db")
        assert pool.has_replica is False

    def test_close_both_pools(self):
        pool = RoutingPool("postgresql://primary/db", read_url="postgresql://replica/db")
        pool._primary = MagicMock()
        pool._replica = MagicMock()
        pool.close()
        pool._primary.close.assert_called_once()
        pool._replica.close.assert_called_once()

    def test_env_var_replica(self):
        with patch.dict("os.environ", {"GRAFOMEM_DB_READ_URL": "postgresql://replica/db"}):
            pool = RoutingPool("postgresql://primary/db")
            assert pool._replica is not None
