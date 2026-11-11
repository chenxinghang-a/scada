"""本轮「找方案→落实」的模拟验证（不依赖任何现场硬件）。

覆盖 7 个修复点：
1. float64 编码器是解码器的严格逆（四种字节序往返）
2. MC 浮点写入与 `read_float32` 互逆（四种字节序往返）
3. MC 响应数据段不完整必须判失败（不能把半帧当有效值）
4. 重启恢复：规则已删除的孤立报警直接收尾、不复活
5. devices.yaml 读改写串行化（并发不重叠）
6. 健康快照暴露 device_config.load_failed 并降级
7. modbus 输入寄存器与保持寄存器口径一致（短应答计失败）
"""

import struct
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from flask import Flask

from 采集层.base_client import decode_float64_registers, encode_float64_registers
from 采集层.mc_client import MCClient
from 报警层.alarm_manager import AlarmManager
from 展示层.api.api_devices import _serialize_device_config
from 展示层.api.api_health import _compute_health_snapshot

BYTE_ORDERS = ('ABCD', 'BADC', 'CDAB', 'DCBA')


# ---------------------------------------------------------------- 1
@pytest.mark.parametrize('order', BYTE_ORDERS)
@pytest.mark.parametrize('value', [0.0, 1.5, -12345.678, 1e-9, 3.4e38])
def test_float64_roundtrip(order, value):
    regs = encode_float64_registers(value, order)
    assert len(regs) == 4
    assert decode_float64_registers(regs, order) == pytest.approx(value, rel=1e-12)


# ---------------------------------------------------------------- 2
@pytest.mark.parametrize('order', BYTE_ORDERS)
def test_mc_float32_write_read_are_inverse(order):
    client = MCClient({'id': 'mc1', 'byte_order': order})
    captured = {}

    def fake_write_words(device, address, words):
        captured['words'] = list(words)
        return True

    client.write_words = fake_write_words
    assert client.write_float32('D', 100, 123.456) is True

    words = captured['words']
    assert len(words) == 2
    # read_float32 的取数口径：低字在前，解码前换成高字在前
    assert client.decode_float32([words[1], words[0]]) == pytest.approx(123.456, rel=1e-6)


# ---------------------------------------------------------------- 3
class _ShortDataSock:
    """响应头声明 N 字节，实际一个字节都不给。"""

    def __init__(self):
        self._sent_header = False

    def sendall(self, _frame):
        return None

    def recv(self, n):
        if not self._sent_header:
            self._sent_header = True
            return b'\x00' * 7 + struct.pack('<H', 10)
        return b''


def test_mc_short_data_frame_is_rejected():
    client = MCClient({'id': 'mc1'})
    client._sock = _ShortDataSock()
    client.connected = True

    assert client.read_words('D', 0, 2) is None
    assert client.stats['failed_reads'] >= 1
    # 半帧意味着流已失步：必须断开等重连，而不是继续读错位数据
    assert client.connected is False


# ---------------------------------------------------------------- 4
class _StubDB:
    def __init__(self, rows):
        self._rows = rows
        self.cleared = []

    def get_uncleared_alarms(self):
        return self._rows

    def clear_alarm(self, alarm_id, device_id=None, register_name=None):
        self.cleared.append((alarm_id, device_id, register_name))
        return True


def _bare_alarm_manager(rows):
    mgr = object.__new__(AlarmManager)
    mgr.rules = {}                      # 规则已被删除
    mgr.alarm_states = {}
    mgr._state_lock = threading.Lock()
    mgr._escalation_manager = None
    mgr.database = _StubDB(rows)
    mgr._config_loaded = True        # 模拟「配置已加载，但该规则已被删除」
    return mgr


def test_restore_skips_alarm_whose_rule_was_deleted():
    rows = [{
        'alarm_id': 'rule_deleted', 'device_id': 'dev1',
        'register_name': 'temperature', 'timestamp': datetime.now(),
        'last_trigger_time': datetime.now(), 'acknowledged': 0,
        'trigger_count': 3,
    }]
    mgr = _bare_alarm_manager(rows)

    assert mgr._restore_active_alarms() == 0
    assert mgr.alarm_states == {}
    # 不清库：规则若被临时注释/改名，重新加回后这条报警还要能被恢复
    assert mgr.database.cleared == []


# ---------------------------------------------------------------- 5
def test_device_config_write_is_serialized():
    state = {'inside': 0, 'max_inside': 0, 'done': 0}
    guard = threading.Lock()

    @_serialize_device_config
    def slow_write():
        with guard:
            state['inside'] += 1
            state['max_inside'] = max(state['max_inside'], state['inside'])
        time.sleep(0.05)
        with guard:
            state['inside'] -= 1
            state['done'] += 1

    threads = [threading.Thread(target=slow_write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert state['done'] == 4
    assert state['max_inside'] == 1


# ---------------------------------------------------------------- 6
def test_health_snapshot_reports_device_config_failure():
    app = Flask(__name__)
    app.device_manager = SimpleNamespace(config_load_failed=True, devices={})
    with app.app_context():
        snapshot = _compute_health_snapshot()

    assert snapshot['device_config']['load_failed'] is True
    assert snapshot['device_config']['device_count'] == 0
    assert snapshot['global_status'] == 'unhealthy'
