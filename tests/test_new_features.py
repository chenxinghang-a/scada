"""
新功能测试 - 覆盖近期添加的核心功能
Round 27: 测试覆盖阶段
"""
import pytest
import time
import threading
from unittest.mock import MagicMock, patch
from datetime import datetime


@pytest.fixture
def mock_db():
    """AlarmManager 依赖的数据库对象（去重逻辑不触碰 DB，用 Mock 即可）"""
    return MagicMock()


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
    """设备状态缓存测试 —— **必须走真实 get_device_status**。

    原实现用 MagicMock(spec=DeviceManager) 自己往 _status_cache 里塞值、
    再断言 `time.time() - 写入时间 < ttl`：这只是在验证算术，删掉
    get_device_status 的整段缓存逻辑它也照样绿（假绿）。
    这里改为构造真实对象、调真实方法，只用替身顶替底层协议客户端。
    """

    @staticmethod
    def _manager(fake_client):
        """真 DeviceManager，但跳过构造（不启连接池/不读 devices.yaml）。

        get_device_status 需要的是 devices / 缓存三件套 / simulation_mode，
        以及 get_client() 拿到的客户端；逐项显式给齐，避免依赖真实配置文件。
        """
        from 采集层.device_manager import DeviceManager
        dm = DeviceManager.__new__(DeviceManager)
        dm.devices = {'dev1': {'name': 'Test Device', 'protocol': 'modbus_tcp',
                               'enabled': True}}
        dm.clients = {}
        dm.simulation_mode = False
        dm._status_cache = {}
        dm._status_cache_time = {}
        dm._status_cache_ttl = 5.0
        dm.get_client = lambda device_id: fake_client
        return dm

    class _FakeClient:
        """只提供 get_device_status 会读的字段，避免 MagicMock 的属性真值干扰。"""

        def __init__(self):
            self.connected = True
            self.stopped = False
            self.stats = {}

        def connect(self):
            return True

    def test_cache_hit_then_expire(self):
        """TTL 内不重复取客户端；过期后必须重新拉取并反映断线。"""
        fake = self._FakeClient()
        calls = []

        dm = self._manager(fake)
        dm.get_client = lambda device_id: (calls.append(device_id), fake)[1]

        first = dm.get_device_status('dev1')
        assert first['connected'] is True, '首次查询应反映底层已连接'
        assert len(calls) == 1

        # TTL 内底层断线，但缓存未过期 → 仍返回缓存值，且**不再**问底层
        fake.connected = False
        hit = dm.get_device_status('dev1')
        assert hit['connected'] is True, 'TTL 内应命中缓存（不是重新查询）'
        assert len(calls) == 1, 'TTL 内重复查询底层说明缓存没生效'

        # 让缓存过期（单调时钟，与生产实现一致）→ 必须重新拉取并反映断线
        dm._status_cache_time['dev1'] -= dm._status_cache_ttl + 1
        expired = dm.get_device_status('dev1')
        assert expired['connected'] is False, '缓存过期后必须重新拉取，不能永远报在线'
        assert len(calls) == 2


class TestWriteIdempotency:
    """写入幂等性测试 —— **必须打真实路由**。

    原实现直接往 _recent_writes 塞时间戳再断言差值：路由里的幂等拦截
    （`_idempotency_begin/_end`）被整段删掉它也不会红。
    这里用 Flask test_client 连打两次同一个写请求，断言底层只下发一次。
    """

    class _FakeControl:
        """安全模块替身：只负责让请求走到幂等之后的真实下发点。"""

        def __init__(self, client):
            self.client = client

        def write_with_verification(self, device_id, address, value, operator, **kw):
            ok = self.client.write_single_register(int(address), int(value))
            return {'success': bool(ok), 'message': '写入成功', 'kind': 'register',
                    'verified': True, 'effective': True, 'warnings': []}

    @staticmethod
    def _fake_client():
        fake = MagicMock()
        fake.connected = True
        fake.calls = 0

        def _write(address, value):
            fake.calls += 1
            return True

        fake.write_single_register = _write
        return fake

    @staticmethod
    def _reset():
        import 展示层.api.api_control as ac
        with ac._write_lock:
            ac._recent_writes.clear()
            ac._in_flight_targets.clear()

    def _post(self, client, auth_headers):
        return client.post('/api/devices/dev1/write-register',
                           json={'address': 100, 'value': 5},
                           headers=auth_headers)

    def test_duplicate_write_within_window(self, app, client, auth_headers, monkeypatch):
        """2 秒内重复提交：第二次不得再下发，且必须明确回"幂等"。"""
        fake = self._fake_client()
        monkeypatch.setattr(app.device_manager, 'get_client', lambda d: fake)
        app.device_control = self._FakeControl(fake)
        self._reset()
        try:
            r1 = self._post(client, auth_headers)
            assert r1.status_code == 200, r1.get_json()
            assert fake.calls == 1, '首次写入应真实下发一次'

            r2 = self._post(client, auth_headers)
            assert fake.calls == 1, '窗口内重复提交又下发了一次 —— 幂等保护失效'
            assert r2.status_code in (200, 409), r2.status_code
            if r2.status_code == 200:
                body = r2.get_json() or {}
                assert body.get('success') is True
                assert '幂等' in (body.get('message') or ''), body
        finally:
            self._reset()

    def test_write_after_window(self, app, client, auth_headers, monkeypatch):
        """窗口过期后必须允许再次下发（不能把所有重试都判成已写入）。"""
        import 展示层.api.api_control as ac

        fake = self._fake_client()
        monkeypatch.setattr(app.device_manager, 'get_client', lambda d: fake)
        app.device_control = self._FakeControl(fake)
        self._reset()
        try:
            assert self._post(client, auth_headers).status_code == 200
            assert fake.calls == 1

            # 把生产代码**实际登记的那个键**拨到窗口之外，不猜键名格式
            with ac._write_lock:
                for k, v in list(ac._recent_writes.items()):
                    if isinstance(v, float):
                        ac._recent_writes[k] = v - ac.IDEMPOTENCY_WINDOW_S - 1

            r2 = self._post(client, auth_headers)
            assert r2.status_code == 200, r2.get_json()
            assert fake.calls == 2, '窗口已过期却仍返回幂等 —— 重试被永久挡死'
        finally:
            self._reset()


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
    """连接池清理测试 —— **必须调真实 _cleanup_once**。

    原实现自己写了一遍"取快照"的列表推导再断言快照里有 1 条：
    清理逻辑里的锁外健康检查 / 锁内删除全都没被执行过，
    即便清理时遍历活视图抛 RuntimeError 它也照样绿。
    """

    def test_cleanup_only_destroys_idle_expired(self):
        from core.connection_pool import ConnectionPool, PooledConnection

        pool = ConnectionPool(factory=lambda k: MagicMock(), max_idle_time=60,
                              name="test-cleanup", auto_scale=False)
        try:
            idle = PooledConnection(MagicMock())
            idle.last_used = time.monotonic() - 120      # 空闲超时
            busy = PooledConnection(MagicMock())
            busy.in_use = True                            # 在用：不能回收
            busy.last_used = time.monotonic()

            with pool._lock:
                pool._pool['conn1'] = idle
                pool._pool['conn2'] = busy

            pool._cleanup_once()

            assert 'conn1' not in pool._pool, '空闲超时的 conn1 应被清理'
            assert 'conn2' in pool._pool, '在用连接 conn2 绝不能被清理'
            idle.client.disconnect.assert_called_once()
            busy.client.disconnect.assert_not_called()
        finally:
            pool.shutdown()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
