"""设备健康的「唯一真值」（round 343）。

## 缺陷

`refresh_device_health()` 在每次 `get_device_health_summary()` 时被调用，
它用 `client.connected` 覆盖健康状态，并**清零**
`consecutive_failures` 与 `_comm_failures`。

`connected` 只说明 **TCP 连着**。现场更常见的是「连着但读不到数据」
（读超时、Modbus 异常码、寄存器不存在）—— 这时 `connected=True`，于是：

* 界面永远显示「连接正常」，实际数据已经几小时没进来；
* `_comm_failures` 是触发设备安全状态（关阀 / 切手动）的**唯一依据**，
  被清零 = 安全状态永不触发。

实测（`tools/e2e_smoke.py` 第 7 步）：停掉从站后采集返回 False、
`last_read_source='cache'`，但健康摘要仍报 `connected`。

## 修法

采集侧（`update_device_health`）是**唯一权威**来源，它写
`last_collect_ok`。`refresh_device_health` 只允许在「采集侧从没报过」时
用 `connected` 做初判；一旦有记录就以采集结果为准，且**不再动失败计数**。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def control():
    """一个最小可用的 DeviceControlSafety（真实实例，mock 掉外部依赖）。"""
    from 智能层.device_control import DeviceControlSafety

    device_manager = MagicMock()
    device_manager.devices = {
        'dev1': {'id': 'dev1', 'name': '测试设备', 'protocol': 'modbus_tcp'},
    }
    device_manager.get_all_devices.return_value = device_manager.devices
    client = MagicMock()
    client.connected = True          # TCP 连着
    device_manager.get_client.return_value = client

    dc = DeviceControlSafety(MagicMock(), device_manager, MagicMock())
    dc.alarm_manager = MagicMock()
    dc.alarm_manager.alarm_output = None
    return dc


class TestHealthSingleSourceOfTruth:
    """采集结果是健康状态的唯一真值。"""

    def test_collect_failure_survives_refresh(self, control):
        """连续失败到阈值 → 刷新后仍是 degraded，不能被 connected 覆盖。

        未达阈值时 `status` 保持原值（避免单次抖动就报警）是**设计**，
        但计数必须能一直累加 —— 见下一条用例。
        """
        for _ in range(control._comm_threshold):
            control.update_device_health('dev1', False, 0)
        assert control._device_health['dev1']['status'] == 'degraded'

        # 每次查汇总都会 refresh —— 这是原缺陷的触发点
        health = control.get_device_health_summary()['devices']['dev1']
        assert health['status'] == 'degraded', (
            f"client.connected=True 把采集失败覆盖成了 {health['status']!r} —— "
            f"界面会显示「连接正常」而实际采不到数据")

    def test_refresh_does_not_clear_failure_counters(self, control):
        """刷新**不许**清零失败计数 —— 它是安全状态的触发依据。"""
        control.update_device_health('dev1', False, 0)
        control.update_device_health('dev1', False, 0)
        failures_before = control._comm_failures.get('dev1')
        assert failures_before == 2

        control.get_device_health_summary()

        assert control._comm_failures.get('dev1') == failures_before, (
            '刷新把 _comm_failures 清零了 —— 连续失败计数永远攒不到阈值，'
            '设备安全状态（关阀/切手动）不会触发')

    def test_collect_success_marks_connected(self, control):
        control.update_device_health('dev1', True, 12.0)
        health = control.get_device_health_summary()['devices']['dev1']
        assert health['status'] == 'connected'
        assert health['last_collect_ok'] is True

    def test_recovery_clears_failures(self, control):
        """只有**成功的采集**才能清零失败计数。"""
        for _ in range(3):
            control.update_device_health('dev1', False, 0)
        assert control._comm_failures.get('dev1') == 3

        control.update_device_health('dev1', True, 10.0)
        assert control._comm_failures.get('dev1') == 0
        assert control._device_health['dev1']['status'] == 'connected'

    def test_first_sight_uses_connected_as_hint(self, control):
        """采集侧还没报过的设备（刚加进来），用 connected 做初判。"""
        health = control.get_device_health_summary()['devices']['dev1']
        assert health.get('last_collect_ok') is None
        assert health['status'] == 'connected'   # client.connected = True

    def test_disconnected_client_marks_disconnected(self, control):
        """TCP 都断了 → 初判就是 disconnected。"""
        control.device_manager.get_client.return_value.connected = False
        health = control.get_device_health_summary()['devices']['dev1']
        assert health['status'] == 'disconnected'

    def test_threshold_triggers_degradation_once(self, control):
        """连续失败到阈值只触发一次降级（边沿，不是每个采样点）。"""
        handler = MagicMock()
        control.register_safe_state_handler('dev1', handler)

        threshold = control._comm_threshold
        for _ in range(threshold + 3):
            control.update_device_health('dev1', False, 0)

        assert handler.call_count == 1, (
            f'安全状态被执行了 {handler.call_count} 次 —— '
            f'应在跨过阈值那一刻只执行一次（否则关阀/声光被反复下发）')
