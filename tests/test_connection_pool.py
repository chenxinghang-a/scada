"""
Tests for core.connection_pool: ConnectionPool and PooledConnection
"""

import threading
import time
import pytest
from unittest.mock import MagicMock, patch

from core.connection_pool import ConnectionPool, PooledConnection


# ============================================================
# PooledConnection Tests
# ============================================================

class TestPooledConnection:

    def test_init_defaults(self):
        """PooledConnection initializes with correct defaults"""
        client = MagicMock()
        conn = PooledConnection(client)

        assert conn.client is client
        assert conn.in_use is False
        assert conn.healthy is True
        assert conn.use_count == 0
        assert conn.created_at > 0
        assert conn.last_used > 0

    def test_slots(self):
        """PooledConnection uses __slots__ for memory efficiency"""
        client = MagicMock()
        conn = PooledConnection(client)
        # Should not have __dict__ (slots-based)
        assert not hasattr(conn, '__dict__')


# ============================================================
# ConnectionPool - Basic Lifecycle
# ============================================================

class TestConnectionPoolLifecycle:

    def test_create_pool(self):
        """Pool initializes with correct stats"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), name="test")
        stats = pool.get_stats()
        assert stats['total'] == 0
        assert stats['created'] == 0
        assert stats['active'] == 0
        assert stats['idle'] == 0
        pool.shutdown()

    def test_acquire_creates_connection(self):
        """acquire creates a new connection when pool is empty"""
        mock_client = MagicMock()
        pool = ConnectionPool(factory=lambda k: mock_client, name="test")

        client = pool.acquire("dev_001")
        assert client is mock_client
        stats = pool.get_stats()
        assert stats['created'] == 1
        assert stats['active'] == 1
        assert stats['misses'] == 1
        pool.shutdown()

    def test_release_makes_idle(self):
        """release moves connection from active to idle"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), name="test")
        pool.acquire("dev_001")
        pool.release("dev_001")

        stats = pool.get_stats()
        assert stats['active'] == 0
        assert stats['idle'] == 1
        pool.shutdown()

    def test_acquire_reuses_idle(self):
        """acquire reuses an idle connection (cache hit)"""
        mock_client = MagicMock()
        pool = ConnectionPool(factory=lambda k: mock_client, name="test")

        pool.acquire("dev_001")
        pool.release("dev_001")

        client2 = pool.acquire("dev_001")
        assert client2 is mock_client
        stats = pool.get_stats()
        assert stats['created'] == 1  # not created twice
        assert stats['hits'] == 1
        pool.shutdown()

    def test_shutdown_destroys_all(self):
        """shutdown destroys all connections"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), name="test")
        pool.acquire("dev_001")
        pool.acquire("dev_002")
        pool.shutdown()

        stats = pool.get_stats()
        assert stats['total'] == 0
        assert stats['destroyed'] == 2


# ============================================================
# ConnectionPool - Max Size
# ============================================================

class TestConnectionPoolMaxSize:

    def test_max_size_blocks_new(self):
        """Pool returns None when max_size is reached"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), max_size=2, name="test")

        c1 = pool.acquire("dev_001")
        c2 = pool.acquire("dev_002")
        c3 = pool.acquire("dev_003")  # should fail

        assert c1 is not None
        assert c2 is not None
        assert c3 is None
        assert pool.get_stats()['total'] == 2
        pool.shutdown()

    def test_release_allows_reacquire(self):
        """After releasing, pool has room for new connections"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), max_size=1, name="test")

        pool.acquire("dev_001")
        pool.release("dev_001")

        # Can't acquire a different key because pool still has dev_001
        c2 = pool.acquire("dev_002")
        assert c2 is None  # pool full (dev_001 still in pool, just idle)

        # But can re-acquire dev_001
        c1_again = pool.acquire("dev_001")
        assert c1_again is not None
        pool.shutdown()


# ============================================================
# ConnectionPool - Factory Errors
# ============================================================

class TestConnectionPoolFactoryErrors:

    def test_factory_returns_none(self):
        """Pool handles factory returning None"""
        pool = ConnectionPool(factory=lambda k: None, name="test")
        client = pool.acquire("dev_001")
        assert client is None
        assert pool.get_stats()['total'] == 0
        pool.shutdown()

    def test_factory_raises_exception(self):
        """Pool handles factory raising exception"""
        def bad_factory(key):
            raise RuntimeError("connection failed")

        pool = ConnectionPool(factory=bad_factory, name="test")
        client = pool.acquire("dev_001")
        assert client is None
        assert pool.get_stats()['total'] == 0
        pool.shutdown()


# ============================================================
# ConnectionPool - Health Check
# ============================================================

class TestConnectionPoolHealthCheck:

    def test_health_check_on_acquire(self):
        """Health check runs on acquire, marks unhealthy connections"""
        mock_client = MagicMock()
        health_fn = MagicMock(return_value=False)

        pool = ConnectionPool(
            factory=lambda k: mock_client,
            health_check=health_fn,
            name="test",
        )

        pool.acquire("dev_001")
        pool.release("dev_001")

        # Next acquire should fail health check
        client = pool.acquire("dev_001")
        # The unhealthy connection gets destroyed, and a new one is created
        # because the factory returns the same mock, it's reused after recreation
        assert health_fn.called
        pool.shutdown()

    def test_health_check_passes(self):
        """Healthy connection is reused normally"""
        mock_client = MagicMock()
        health_fn = MagicMock(return_value=True)

        pool = ConnectionPool(
            factory=lambda k: mock_client,
            health_check=health_fn,
            name="test",
        )

        pool.acquire("dev_001")
        pool.release("dev_001")

        client = pool.acquire("dev_001")
        assert client is mock_client
        pool.shutdown()


# ============================================================
# ConnectionPool - Lifetime / Idle Expiry
# ============================================================

class TestConnectionPoolExpiry:

    def test_max_lifetime_expiry(self):
        """Connections are expired when max_lifetime is exceeded"""
        pool = ConnectionPool(
            factory=lambda k: MagicMock(),
            max_lifetime=0.1,  # 100ms
            name="test",
        )

        pool.acquire("dev_001")
        pool.release("dev_001")

        # Wait for lifetime to expire
        time.sleep(0.2)

        # Acquire should create a new connection (old one expired)
        client = pool.acquire("dev_001")
        assert client is not None
        stats = pool.get_stats()
        assert stats['created'] == 2  # original + replacement
        pool.shutdown()

    def test_max_idle_time_expiry(self):
        """Connections are expired when idle too long"""
        pool = ConnectionPool(
            factory=lambda k: MagicMock(),
            max_idle_time=0.1,  # 100ms
            name="test",
        )

        pool.acquire("dev_001")
        pool.release("dev_001")

        # Wait for idle timeout
        time.sleep(0.2)

        # Acquire should create new connection
        client = pool.acquire("dev_001")
        assert client is not None
        stats = pool.get_stats()
        assert stats['created'] == 2
        pool.shutdown()


# ============================================================
# ConnectionPool - Unhealthy Release
# ============================================================

class TestConnectionPoolUnhealthyRelease:

    def test_release_unhealthy(self):
        """Releasing with healthy=False marks connection as unhealthy"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), name="test")
        pool.acquire("dev_001")
        pool.release("dev_001", healthy=False)

        stats = pool.get_stats()
        assert stats['idle'] == 1

        # Acquiring an unhealthy connection should trigger re-creation
        client = pool.acquire("dev_001")
        assert client is not None
        pool.shutdown()


# ============================================================
# ConnectionPool - Disconnect / Close Detection
# ============================================================

class TestConnectionPoolDisconnect:

    def test_destroy_calls_disconnect(self):
        """destroy calls client.disconnect() if available"""
        mock_client = MagicMock()
        pool = ConnectionPool(factory=lambda k: mock_client, name="test")

        pool.acquire("dev_001")
        pool.shutdown()

        mock_client.disconnect.assert_called_once()

    def test_destroy_calls_close_fallback(self):
        """destroy calls client.close() if disconnect not available"""
        mock_client = MagicMock(spec=['close'])  # no disconnect method
        pool = ConnectionPool(factory=lambda k: mock_client, name="test")

        pool.acquire("dev_001")
        pool.shutdown()

        mock_client.close.assert_called_once()


# ============================================================
# ConnectionPool - Remove / Contains
# ============================================================

class TestConnectionPoolRemoveContains:

    def test_remove_existing(self):
        """remove destroys and removes a specific connection"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), name="test")
        pool.acquire("dev_001")

        result = pool.remove("dev_001")
        assert result is True
        assert pool.get_stats()['total'] == 0

    def test_remove_nonexistent(self):
        """remove returns False for unknown key"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), name="test")
        result = pool.remove("nonexistent")
        assert result is False

    def test_contains(self):
        """contains checks if key is in pool"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), name="test")
        pool.acquire("dev_001")

        assert pool.contains("dev_001") is True
        assert pool.contains("dev_002") is False
        assert "dev_001" in pool
        assert "dev_002" not in pool
        pool.shutdown()

    def test_len(self):
        """len returns number of connections in pool"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), name="test")
        assert len(pool) == 0

        pool.acquire("dev_001")
        assert len(pool) == 1

        pool.acquire("dev_002")
        assert len(pool) == 2
        pool.shutdown()


# ============================================================
# ConnectionPool - Thread Safety
# ============================================================

class TestConnectionPoolThreadSafety:

    def test_concurrent_acquire_release(self):
        """Concurrent acquire/release doesn't corrupt state"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), max_size=50, name="test")
        errors = []

        def worker(device_id):
            try:
                for _ in range(20):
                    client = pool.acquire(device_id)
                    if client:
                        pool.release(device_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"dev_{i:03d}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0
        stats = pool.get_stats()
        assert stats['created'] > 0
        pool.shutdown()

    def test_concurrent_same_key(self):
        """Concurrent acquire on same key is safe"""
        mock_client = MagicMock()
        pool = ConnectionPool(factory=lambda k: mock_client, max_size=1, name="test")
        results = []
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait(timeout=5)
            client = pool.acquire("shared_dev")
            results.append(client is not None)
            if client:
                time.sleep(0.01)
                pool.release("shared_dev")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # At least one should succeed
        assert any(results)
        pool.shutdown()


# ============================================================
# ConnectionPool - Repr
# ============================================================

class TestConnectionPoolRepr:

    def test_repr(self):
        """repr returns readable string"""
        pool = ConnectionPool(factory=lambda k: MagicMock(), name="test_repr")
        r = repr(pool)
        assert "test_repr" in r
        assert "total=" in r
        pool.shutdown()


# ============================================================
# DeviceManager Integration
# ============================================================

class TestDeviceManagerPoolIntegration:

    @pytest.fixture
    def devices_yaml(self, tmp_path):
        """Create a temporary devices.yaml config file"""
        import yaml
        config = {
            'devices': [
                {
                    'id': 'pump_01',
                    'name': 'Test Pump',
                    'protocol': 'modbus_tcp',
                    'host': '127.0.0.1',
                    'port': 502,
                    'enabled': True,
                    'registers': []
                },
                {
                    'id': 'motor_01',
                    'name': 'Test Motor',
                    'protocol': 'modbus_tcp',
                    'host': '127.0.0.1',
                    'port': 503,
                    'enabled': True,
                    'registers': []
                },
            ]
        }
        cfg_file = tmp_path / 'devices.yaml'
        with open(cfg_file, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True)
        return str(cfg_file)

    @pytest.fixture
    def device_manager(self, devices_yaml):
        from 采集层.device_manager import DeviceManager
        return DeviceManager(config_path=devices_yaml, simulation_mode=True, use_enhanced_simulation=False)

    def test_pool_created(self, device_manager):
        """DeviceManager creates a connection pool on init"""
        assert device_manager._connection_pool is not None
        assert device_manager._connection_pool._name == "modbus"

    def test_get_client_uses_pool(self, device_manager):
        """get_client acquires from pool"""
        client = device_manager.get_client("pump_01")
        assert client is not None
        # Pool should have the connection
        assert device_manager._connection_pool.contains("pump_01")

    def test_get_client_caches(self, device_manager):
        """get_client returns same instance on repeated calls"""
        c1 = device_manager.get_client("pump_01")
        c2 = device_manager.get_client("pump_01")
        assert c1 is c2

    def test_disconnect_removes_from_pool(self, device_manager):
        """disconnect_device removes connection from pool"""
        device_manager.get_client("pump_01")
        device_manager.disconnect_device("pump_01")
        assert not device_manager._connection_pool.contains("pump_01")

    def test_get_pool_stats(self, device_manager):
        """get_pool_stats returns pool statistics"""
        device_manager.get_client("pump_01")
        stats = device_manager.get_pool_stats()
        assert 'created' in stats
        assert 'active' in stats
        assert stats['created'] >= 1
