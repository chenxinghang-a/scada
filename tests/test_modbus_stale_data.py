"""
回归测试：Modbus 读失败静默返回陈旧缓存（"静默假成功"）

缺陷（修复前）：
``ModbusClient.read_holding_registers()`` / ``read_input_registers()`` 在全部
5 条失败路径上都 ``return self._last_good_values.get(cache_key)``。返回值本身
与一次成功的实时读取**完全无法区分**，导致：
  1. 操作员看到"看起来正常"的数据，实际设备早已失联；
  2. 上游熔断器因为"每次都有返回值"永远不跳闸；
  3. 系统失效模式从"报错"变成"静默假死"。

修复后必须满足的契约：
  - 每次读操作结束都更新 ``last_read_source`` ('fresh'/'cache'/'none') 与
    ``last_read_ok``；
  - 每条"用缓存兜底返回"的路径都自增 ``stats['stale_reads']``；
  - ``get_cache_age(address, count)`` 能给出缓存年龄（秒）；
  - 采集侧把缓存兜底值标为 ``UNCERTAIN_LAST_USABLE``(64) 而非 ``GOOD``(192)。
"""

import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from pymodbus.exceptions import ConnectionException

from 采集层.data_collector import DataCollector, DataQualityAssessor
from 采集层.modbus_client import ModbusClient


# ============================================================
# 底层 pymodbus 客户端替身
# ============================================================

class _FakeResult:
    """模拟 pymodbus 的响应对象（只用到 registers / isError）"""

    def __init__(self, registers=None, error=False):
        self.registers = list(registers or [])
        self._error = error

    def isError(self):
        return self._error


class _FakeInnerClient:
    """可切换 成功 / 错误响应 / 抛异常 三种行为的底层客户端替身"""

    HOLDING_VALUES = [11, 22]
    INPUT_VALUES = [33, 44]

    def __init__(self):
        self.return_error = False
        self.exc = None

    def _respond(self, values):
        if self.exc is not None:
            raise self.exc
        return _FakeResult(values, error=self.return_error)

    def read_holding_registers(self, address, count, slave=None):
        return self._respond(self.HOLDING_VALUES)

    def read_input_registers(self, address, count, slave=None):
        return self._respond(self.INPUT_VALUES)


@pytest.fixture
def modbus_client():
    """一个已"连接"的 ModbusClient，底层换成可控替身"""
    c = ModbusClient({
        'id': 'dev_stale',
        'name': '陈旧测试设备',
        'protocol': 'modbus_tcp',
        'host': '127.0.0.1',
        'port': 502,
    })
    c.client = _FakeInnerClient()
    c.connected = True
    # ConnectionException 路径连续失败 3 次会触发真实网络重连，本测试只关心
    # 陈旧性标记，把它换成 no-op 避免无意义的网络等待。
    c.reconnect = lambda: False
    return c


# ============================================================
# 保持寄存器（FC03）
# ============================================================

class TestHoldingRegisterStaleness:

    def test_success_marks_fresh(self, modbus_client):
        """成功读取：写入缓存、标记 fresh、stale_reads 保持 0"""
        values = modbus_client.read_holding_registers(0, 2)

        assert values == _FakeInnerClient.HOLDING_VALUES
        assert modbus_client.last_read_source == 'fresh'
        assert modbus_client.last_read_ok is True
        assert modbus_client.stats['stale_reads'] == 0

    def test_get_cache_age_after_success(self, modbus_client):
        """成功读取后缓存年龄是一个 >= 0 的浮点数"""
        modbus_client.read_holding_registers(0, 2)

        age = modbus_client.get_cache_age(0, 2)
        assert isinstance(age, float)
        assert age >= 0.0

    def test_get_cache_age_none_when_never_read(self, modbus_client):
        """从未成功读取过的地址，缓存年龄为 None"""
        assert modbus_client.get_cache_age(0, 2) is None

    def test_connection_exception_returns_stale_cache(self, modbus_client):
        """连接异常：返回值等于缓存，但被明确标记为陈旧"""
        fresh = modbus_client.read_holding_registers(0, 2)
        assert modbus_client.last_read_source == 'fresh'

        time.sleep(0.02)  # 让缓存年龄可观测地 > 0
        modbus_client.client.exc = ConnectionException('连接被对端重置')

        stale = modbus_client.read_holding_registers(0, 2)

        assert stale == fresh  # 向后兼容：返回值内容不变
        assert modbus_client.last_read_source == 'cache'
        assert modbus_client.last_read_ok is False
        assert modbus_client.stats['stale_reads'] == 1

        age = modbus_client.get_cache_age(0, 2)
        assert age is not None
        assert age > 0.0

    def test_error_response_returns_stale_cache(self, modbus_client):
        """Modbus 错误响应（isError()==True）：同样标记为陈旧"""
        modbus_client.read_holding_registers(0, 2)

        modbus_client.client.return_error = True
        stale = modbus_client.read_holding_registers(0, 2)

        assert stale == _FakeInnerClient.HOLDING_VALUES
        assert modbus_client.last_read_source == 'cache'
        assert modbus_client.last_read_ok is False
        assert modbus_client.stats['stale_reads'] == 1

    def test_not_connected_returns_stale_cache(self, modbus_client):
        """设备未连接：有缓存则返回缓存并标记陈旧"""
        modbus_client.read_holding_registers(0, 2)

        modbus_client.connected = False
        stale = modbus_client.read_holding_registers(0, 2)

        assert stale == _FakeInnerClient.HOLDING_VALUES
        assert modbus_client.last_read_source == 'cache'
        assert modbus_client.last_read_ok is False
        assert modbus_client.stats['stale_reads'] == 1

    def test_unexpected_exception_returns_stale_cache(self, modbus_client):
        """非 ConnectionException 的意外异常：同样标记为陈旧"""
        modbus_client.read_holding_registers(0, 2)

        modbus_client.client.exc = RuntimeError('解析响应失败')
        stale = modbus_client.read_holding_registers(0, 2)

        assert stale == _FakeInnerClient.HOLDING_VALUES
        assert modbus_client.last_read_source == 'cache'
        assert modbus_client.stats['stale_reads'] == 1

    def test_no_cache_failure_returns_none(self, modbus_client):
        """无缓存时失败：返回 None，且不得伪造成 fresh"""
        modbus_client.client.exc = ConnectionException('从未成功连接过')

        result = modbus_client.read_holding_registers(0, 2)

        assert result is None
        assert modbus_client.last_read_source == 'none'
        assert modbus_client.last_read_ok is False
        assert modbus_client.stats['stale_reads'] == 0

    def test_no_cache_error_response_returns_none(self, modbus_client):
        """无缓存 + 错误响应：返回 None"""
        modbus_client.client.return_error = True

        result = modbus_client.read_holding_registers(0, 2)

        assert result is None
        assert modbus_client.last_read_source == 'none'

    def test_invalid_address_returns_none_without_faking_fresh(self, modbus_client):
        """地址越界：返回 None 并标记 none（绝不能悄悄返回别的值）"""
        result = modbus_client.read_holding_registers(70000, 2)

        assert result is None
        assert modbus_client.last_read_source == 'none'
        assert modbus_client.last_read_ok is False

    def test_stale_reads_accumulate_and_success_resets_source(self, modbus_client):
        """stale_reads 累加；恢复成功后 last_read_source 回到 fresh"""
        modbus_client.read_holding_registers(0, 2)

        modbus_client.client.exc = ConnectionException('断线')
        modbus_client.read_holding_registers(0, 2)
        modbus_client.read_holding_registers(0, 2)
        assert modbus_client.stats['stale_reads'] == 2
        assert modbus_client.last_read_source == 'cache'

        modbus_client.client.exc = None
        modbus_client.read_holding_registers(0, 2)
        assert modbus_client.last_read_source == 'fresh'
        assert modbus_client.last_read_ok is True
        assert modbus_client.stats['stale_reads'] == 2  # 历史计数不清零

    def test_cache_is_per_address_count(self, modbus_client):
        """缓存按 (address, count) 区分，避免错误地址串用别人的缓存"""
        modbus_client.read_holding_registers(0, 2)

        modbus_client.client.exc = ConnectionException('断线')
        assert modbus_client.read_holding_registers(4, 2) is None
        assert modbus_client.get_cache_age(4, 2) is None
        assert modbus_client.get_cache_age(0, 2) is not None


# ============================================================
# 输入寄存器（FC04）—— 同构路径必须同样受保护
# ============================================================

class TestInputRegisterStaleness:

    def test_success_marks_fresh(self, modbus_client):
        values = modbus_client.read_input_registers(0, 2)

        assert values == _FakeInnerClient.INPUT_VALUES
        assert modbus_client.last_read_source == 'fresh'
        assert modbus_client.last_read_ok is True
        assert modbus_client.stats['stale_reads'] == 0

    def test_connection_exception_returns_stale_cache(self, modbus_client):
        fresh = modbus_client.read_input_registers(0, 2)

        time.sleep(0.02)
        modbus_client.client.exc = ConnectionException('连接被对端重置')
        stale = modbus_client.read_input_registers(0, 2)

        assert stale == fresh
        assert modbus_client.last_read_source == 'cache'
        assert modbus_client.last_read_ok is False
        assert modbus_client.stats['stale_reads'] == 1
        assert modbus_client.get_cache_age(0, 2) > 0.0

    def test_error_response_returns_stale_cache(self, modbus_client):
        modbus_client.read_input_registers(0, 2)

        modbus_client.client.return_error = True
        stale = modbus_client.read_input_registers(0, 2)

        assert stale == _FakeInnerClient.INPUT_VALUES
        assert modbus_client.last_read_source == 'cache'
        assert modbus_client.stats['stale_reads'] == 1

    def test_not_connected_returns_stale_cache(self, modbus_client):
        modbus_client.read_input_registers(0, 2)

        modbus_client.connected = False
        stale = modbus_client.read_input_registers(0, 2)

        assert stale == _FakeInnerClient.INPUT_VALUES
        assert modbus_client.last_read_source == 'cache'
        assert modbus_client.stats['stale_reads'] == 1

    def test_no_cache_failure_returns_none(self, modbus_client):
        modbus_client.client.exc = ConnectionException('从未成功连接过')

        result = modbus_client.read_input_registers(0, 2)

        assert result is None
        assert modbus_client.last_read_source == 'none'
        assert modbus_client.last_read_ok is False
        assert modbus_client.stats['stale_reads'] == 0

    def test_unexpected_exception_returns_stale_cache(self, modbus_client):
        modbus_client.read_input_registers(0, 2)

        modbus_client.client.exc = RuntimeError('解析响应失败')
        stale = modbus_client.read_input_registers(0, 2)

        assert stale == _FakeInnerClient.INPUT_VALUES
        assert modbus_client.last_read_source == 'cache'
        assert modbus_client.stats['stale_reads'] == 1

    def test_stale_reads_accumulate(self, modbus_client):
        modbus_client.read_input_registers(0, 2)

        modbus_client.client.exc = ConnectionException('断线')
        modbus_client.read_input_registers(0, 2)
        modbus_client.read_input_registers(0, 2)

        assert modbus_client.stats['stale_reads'] == 2


# ============================================================
# 陈旧性传播到数据质量码（采集层）
# ============================================================

class TestStaleQualityPropagation:
    """缓存兜底值必须以 UNCERTAIN_LAST_USABLE(64) 落库，而不是 GOOD(192)"""

    DEVICE_ID = 'dev_stale'

    @pytest.fixture
    def collector_and_db(self, tmp_path, monkeypatch):
        # DiskBackedQueue 会在构造时从持久化文件恢复数据，同一个目录会让
        # 上个用例入队的数据污染本用例。每个用例独占一个目录。
        monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(tmp_path / 'queue'))
        dm = MagicMock()
        dm.get_device_status.return_value = {
            'connected': True, 'stats': {'state': 'running'}
        }
        db = MagicMock()
        collector = DataCollector(dm, db)
        return collector, db

    @staticmethod
    def _device_config():
        return {
            'registers': [
                {'name': 'temp', 'address': 0, 'data_type': 'uint16', 'unit': 'C'},
            ]
        }

    def _run_process_data_once(self, collector, db, timeout=10.0):
        """在后台线程跑 _process_data，直到数据库批量写入被调用"""
        collector.running = True
        thread = threading.Thread(target=collector._process_data, daemon=True)
        thread.start()
        try:
            deadline = time.time() + timeout
            while time.time() < deadline and not db.insert_data_batch.called:
                time.sleep(0.05)
        finally:
            collector.running = False
            thread.join(timeout=5)

        assert db.insert_data_batch.called, "数据未在超时前进入数据库批次"
        return db.insert_data_batch.call_args[0][0]

    def test_collect_modbus_flags_stale_item(self, modbus_client, collector_and_db):
        """读取走缓存兜底时，入队数据必须带 stale 标记"""
        collector, _ = collector_and_db
        modbus_client.read_holding_registers(0, 1)  # 先写入缓存

        modbus_client.client.exc = ConnectionException('断线')
        collector._collect_modbus(
            modbus_client, self.DEVICE_ID, self._device_config(), datetime.now()
        )

        item = collector.data_queue.get_nowait()
        assert item['stale'] is True
        assert item['register_name'] == 'temp'
        assert modbus_client.last_read_source == 'cache'

    def test_collect_modbus_does_not_flag_fresh_item(self, modbus_client, collector_and_db):
        """正常读取不得被误标为陈旧"""
        collector, _ = collector_and_db

        collector._collect_modbus(
            modbus_client, self.DEVICE_ID, self._device_config(), datetime.now()
        )

        item = collector.data_queue.get_nowait()
        assert 'stale' not in item
        assert modbus_client.last_read_source == 'fresh'

    def test_collect_modbus_tolerates_client_without_flag(self, collector_and_db):
        """不支持 last_read_source 的客户端（如模拟客户端）视为新鲜数据"""
        collector, _ = collector_and_db

        class _NoFlagClient:
            connected = True

            @staticmethod
            def read_holding_registers(address, count):
                return [7]

            @staticmethod
            def decode_uint16(register):
                return register & 0xFFFF

        collector._collect_modbus(
            _NoFlagClient(), self.DEVICE_ID, self._device_config(), datetime.now()
        )

        item = collector.data_queue.get_nowait()
        assert 'stale' not in item

    def test_stale_item_gets_uncertain_last_usable_quality(self, collector_and_db):
        """核心断言：陈旧缓存值的质量码是 64 而不是 192"""
        collector, db = collector_and_db

        collector.data_queue.put_nowait({
            'device_id': self.DEVICE_ID,
            'register_name': 'temp',
            'value': 25.0,
            'timestamp': datetime.now(),
            'unit': 'C',
            'stale': True,
        })

        batch = self._run_process_data_once(collector, db)

        assert batch[0]['quality'] == DataQualityAssessor.UNCERTAIN_LAST_USABLE
        assert batch[0]['quality'] == 64
        assert batch[0]['quality'] != DataQualityAssessor.GOOD

    def test_fresh_item_keeps_good_quality(self, collector_and_db):
        """对照组：新鲜数据仍然是 GOOD(192)，不能一刀切降级"""
        collector, db = collector_and_db

        collector.data_queue.put_nowait({
            'device_id': self.DEVICE_ID,
            'register_name': 'temp',
            'value': 25.0,
            'timestamp': datetime.now(),
            'unit': 'C',
        })

        batch = self._run_process_data_once(collector, db)

        assert batch[0]['quality'] == DataQualityAssessor.GOOD
        assert batch[0]['quality'] == 192
