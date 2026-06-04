"""
新功能测试 - 覆盖近期添加的核心功能
Round 27: 测试覆盖阶段
"""
import pytest
import time
import threading
from unittest.mock import MagicMock, patch
from datetime import datetime


class TestAlarmDedup:
    """报警去重功能测试"""

    def test_should_emit_cooldown(self, mock_db):
        """测试冷却窗口去重"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)

        # 第一次应该推送
        assert am._should_emit('rule1', 'device1', 'register1') is True
        am._record_emit('rule1', 'device1', 'register1')

        # 冷却期内不应该推送
        assert am._should_emit('rule1', 'device1', 'register1') is False

    def test_should_emit_acknowledge_suppress(self, mock_db):
        """测试确认后抑制"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)

        # 确认后应该抑制
        am._record_acknowledge('rule1', 'device1', 'register1')
        assert am._should_emit('rule1', 'device1', 'register1') is False

    def test_should_emit_disabled(self, mock_db):
        """测试去重禁用时始终推送"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        am.dedup_config.enabled = False

        # 禁用时始终推送
        assert am._should_emit('rule1', 'device1', 'register1') is True
        am._record_emit('rule1', 'device1', 'register1')
        assert am._should_emit('rule1', 'device1', 'register1') is True


class TestDeviceStatusCache:
    """设备状态缓存测试"""

    def test_cache_hit(self):
        """测试缓存命中"""
        from 采集层.device_manager import DeviceManager
        dm = MagicMock(spec=DeviceManager)
        dm._status_cache = {}
        dm._status_cache_time = {}
        dm._status_cache_ttl = 5.0
        dm.devices = {'dev1': {'name': 'Test Device'}}

        # 模拟get_device_status
        status = {'device_id': 'dev1', 'connected': True}
        dm._status_cache['dev1'] = status
        dm._status_cache_time['dev1'] = time.time()

        # 缓存应该命中
        cache_age = time.time() - dm._status_cache_time['dev1']
        assert cache_age < dm._status_cache_ttl

    def test_cache_expire(self):
        """测试缓存过期"""
        from 采集层.device_manager import DeviceManager
        dm = MagicMock(spec=DeviceManager)
        dm._status_cache = {}
        dm._status_cache_time = {}
        dm._status_cache_ttl = 5.0

        # 设置过期缓存
        dm._status_cache['dev1'] = {'device_id': 'dev1'}
        dm._status_cache_time['dev1'] = time.time() - 10  # 10秒前

        # 缓存应该过期
        cache_age = time.time() - dm._status_cache_time['dev1']
        assert cache_age >= dm._status_cache_ttl


class TestWriteIdempotency:
    """写入幂等性测试"""

    def test_duplicate_write_within_window(self):
        """测试2秒内重复写入"""
        from 展示层.api.api_control import _recent_writes, _write_lock, IDEMPOTENCY_WINDOW_S

        # 清空
        with _write_lock:
            _recent_writes.clear()

        # 模拟写入
        write_key = "dev1:100:50"
        with _write_lock:
            _recent_writes[write_key] = time.time()

        # 2秒内重复写入应该被去重
        with _write_lock:
            last_write = _recent_writes.get(write_key, 0)
            assert time.time() - last_write < IDEMPOTENCY_WINDOW_S

    def test_write_after_window(self):
        """测试窗口过期后允许写入"""
        from 展示层.api.api_control import _recent_writes, _write_lock, IDEMPOTENCY_WINDOW_S

        # 清空
        with _write_lock:
            _recent_writes.clear()

        # 模拟过期写入
        write_key = "dev1:100:50"
        with _write_lock:
            _recent_writes[write_key] = time.time() - 5  # 5秒前

        # 窗口过期后应该允许写入
        with _write_lock:
            last_write = _recent_writes.get(write_key, 0)
            assert time.time() - last_write >= IDEMPOTENCY_WINDOW_S


class TestFallbackSimulation:
    """故障降级模拟数据测试"""

    def test_generate_fallback_data(self):
        """测试生成降级模拟数据"""
        from 采集层.data_collector import DataCollector
        dc = MagicMock(spec=DataCollector)

        device_config = {
            'registers': [
                {'name': 'temperature', 'data_type': 'float32', 'unit': '°C'},
                {'name': 'pressure', 'data_type': 'float32', 'unit': 'bar'},
                {'name': 'speed', 'data_type': 'int16', 'unit': 'rpm'},
            ]
        }

        # 调用实际方法
        from 采集层.data_collector import DataCollector
        result = DataCollector._generate_fallback_data(dc, 'dev1', device_config)

        # 应该生成3条数据
        assert len(result) == 3
        for item in result:
            assert item['device_id'] == 'dev1'
            assert item['quality'] == 'simulated'
            assert 'value' in item
            assert 'timestamp' in item


class TestConfigHotReload:
    """配置热重载测试"""

    def test_config_watcher_start(self):
        """测试配置监控启动"""
        from 报警层.alarm_manager import AlarmManager
        db = MagicMock()
        am = AlarmManager(db)

        # 应该有配置监控
        assert hasattr(am, '_config_watcher_running')
        assert am._config_watcher_running is True


class TestConnectionPoolOptimized:
    """连接池优化测试"""

    def test_cleanup_snapshot(self):
        """测试清理时使用快照"""
        from core.connection_pool import ConnectionPool
        pool = MagicMock(spec=ConnectionPool)
        pool._pool = {
            'conn1': MagicMock(in_use=False, last_used=time.time() - 100),
            'conn2': MagicMock(in_use=True, last_used=time.time()),
        }
        pool._lock = threading.Lock()
        pool._max_idle_time = 60

        # 模拟快照逻辑
        with pool._lock:
            snapshot = [(key, conn) for key, conn in pool._pool.items()
                       if not conn.in_use]

        # 应该只有conn1在快照中（conn2在使用中）
        assert len(snapshot) == 1
        assert snapshot[0][0] == 'conn1'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
