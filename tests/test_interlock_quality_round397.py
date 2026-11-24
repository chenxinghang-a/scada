# -*- coding: utf-8 -*-
"""round 397：联锁遇到不可信数据（质量码 + 时间戳是强制契约）。

## 缺陷

`check_interlocks(device_id, name, value)` 签名里没有数据质量与采样时间。
采集断线后快照冻结在最后一帧，联锁看到的是"断线前正常"的值 ——
等于联锁失效：操作员可以在传感器已经失联的情况下启动受保护的设备。

## 修法（方案 A，控制改造面）

* `check_interlocks(..., quality=..., updated_at=...)`：两者都是**强制契约**；
* **默认不可信**（`quality=None` → `QUALITY_MISSING`，`updated_at=None` →
  `TIMESTAMP_MISSING`）—— 给"正常"的默认值会让漏改的调用点静默穿透；
* 质量码非 GOOD（只认白名单 192 / `'GOOD'` 等）或
  `now - updated_at > 10s` → 不触发、不解除，并记录受影响规则；
* 启机/写入准入（`batch_control` / `validate_write`）对"数据不可信"的
  block_start 联锁 **fail-closed 拦截**，返回体与日志都带原因码。

本文件同时覆盖采集层接线：`_dispatch_intelligence` 必须把
`quality` / `timestamp` 一起传给联锁（否则默认不可信会让联锁永不触发）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

GOOD_CODE = 192          # 采集层 DataQualityAssessor.GOOD（OPC UA 0xC0）


class _EchoClient:
    def __init__(self):
        self.connected = True
        self.last_read_source = 'fresh'
        self.coils: dict[int, bool] = {}
        self.registers: dict[int, int] = {}
        self.write_calls: list[tuple] = []

    def write_single_coil(self, address, value):
        self.write_calls.append(('coil', int(address), bool(value)))
        self.coils[int(address)] = bool(value)
        return True

    def write_single_register(self, address, value):
        self.write_calls.append(('register', int(address), int(value)))
        self.registers[int(address)] = int(value)
        return True

    def read_coils(self, address, count=1):
        return [self.coils.get(int(address), False)]

    def read_holding_registers(self, address, count=1):
        return [self.registers.get(int(address) + i, 0) for i in range(count)]


class _Manager:
    """传感器 `sensor_01` + 受保护的机械设备 `press_01`。"""

    def __init__(self, client=None):
        self.devices = {
            'sensor_01': {'protocol': 'modbus_tcp', 'device_category': 'instrument'},
            'press_01': {
                'protocol': 'modbus_tcp',
                'device_category': 'mechanical',
                'registers': [{'name': 'motor_enable', 'address': 10}],
                'control': {
                    'start': {'type': 'coil', 'address': 1, 'value': True},
                    'stop': {'type': 'coil', 'address': 2, 'value': False},
                },
            },
        }
        self.simulation_mode = True
        self.client = client or _EchoClient()
        self.stopped = {'press_01'}

    def get_all_devices(self):
        return self.devices

    def get_client(self, _device_id):
        return self.client

    def get_device_status(self, device_id):
        return {'device_id': device_id, 'stopped': device_id in self.stopped}

    def start_device(self, device_id, write=True):
        self.stopped.discard(device_id)
        return True

    def stop_device(self, device_id, write=True):
        self.stopped.add(device_id)
        return True

    def set_estop_override(self, active):
        pass


def _block_rule(action='block_start'):
    action_cfg = {'type': action, 'device_id': 'press_01'}
    if action == 'write_register':
        action_cfg.update({'register': 'motor_enable', 'value': 0})
    return {
        'id': 'il_temp', 'name': '超温禁启', 'enabled': True, 'priority': 1,
        'condition': {'type': 'threshold', 'device_id': 'sensor_01',
                      'register': 'temp', 'operator': '>', 'value': 120},
        'action': action_cfg,
    }


def _dc(rules=None):
    from 智能层.device_control import DeviceControlSafety
    manager = _Manager()
    dc = DeviceControlSafety(
        MagicMock(), manager, None,
        config={'interlocks': rules if rules is not None else [_block_rule()]})
    return dc, manager


def _start(dc):
    return dc.batch_control('start', 'op', device_ids=['press_01'])


# ---------------------------------------------------------------------------
# 1. 默认不可信（fail-closed）—— 这条最防"漏改调用点静默穿透"
# ---------------------------------------------------------------------------

def test_missing_quality_is_untrusted_by_default():
    dc, _ = _dc()

    res = dc.check_interlocks('sensor_01', 'temp', 25.0)

    assert res is not None, '不传质量码时不应按"正常"静默评估'
    assert res['trusted'] is False
    assert res['reason_code'] == 'QUALITY_MISSING'


def test_missing_timestamp_is_untrusted():
    dc, _ = _dc()

    res = dc.check_interlocks('sensor_01', 'temp', 25.0, quality='GOOD')

    assert res['trusted'] is False
    assert res['reason_code'] == 'TIMESTAMP_MISSING'


def test_bad_quality_is_untrusted():
    dc, _ = _dc()

    res = dc.check_interlocks('sensor_01', 'temp', 25.0,
                              quality='BAD', updated_at=time.time())

    assert res['trusted'] is False
    assert res['reason_code'] == 'QUALITY_UNTRUSTED'


def test_uncertain_quality_code_is_untrusted():
    """UNCERTAIN_LAST_USABLE(64) 不能按"不是 BAD 就算好"放行。"""
    dc, _ = _dc()

    res = dc.check_interlocks('sensor_01', 'temp', 25.0,
                              quality=64, updated_at=time.time())

    assert res['trusted'] is False
    assert res['reason_code'] == 'QUALITY_UNTRUSTED'


# ---------------------------------------------------------------------------
# 2. 验收：断线超过 10s → 启动受保护设备必须被拒（返回体 + 日志）
# ---------------------------------------------------------------------------

def test_stale_snapshot_blocks_protected_device_start(caplog):
    dc, manager = _dc()

    with caplog.at_level('WARNING'):
        res = dc.check_interlocks(
            'sensor_01', 'temp', 25.0,          # 值本身"正常"，但已冻结 30s
            quality=GOOD_CODE,
            updated_at=time.time() - 30,
        )

    assert res['trusted'] is False
    assert res['reason_code'] == 'DATA_STALE'
    assert res['age_s'] >= 30
    assert any('DATA_STALE' in r.getMessage() for r in caplog.records), (
        '数据超龄没有留下明确日志 —— 运维无法从日志看出联锁为何不动作')

    started = _start(dc)

    assert started['success'] is False
    entry = started['results']['press_01']
    assert entry['success'] is False
    assert entry['data_untrusted'] is True
    assert entry['reason_code'] == 'DATA_STALE'
    assert '不可信' in entry['message']
    assert manager.client.write_calls == [], (
        '数据已超龄，仍然向受保护的 mechanical 设备下发了启动')


def test_stale_snapshot_blocks_validate_write():
    dc, _ = _dc()
    dc.check_interlocks('sensor_01', 'temp', 25.0,
                        quality=GOOD_CODE, updated_at=time.time() - 30)

    res = dc.validate_write('press_01', 1, 1, 'op', control_point=True)

    assert res['allowed'] is False
    assert res['data_untrusted'] is True
    assert res['reason_code'] == 'DATA_STALE'


def test_collector_dispatch_forwards_quality_and_timestamp():
    """采集层必须把质量/时间戳一起传给联锁（否则默认不可信）。

    这条走真实的 `_dispatch_intelligence`，模拟"断线 30s、快照冻结在正常值"。
    """
    from 采集层.data_collector import DataCollector

    dc, manager = _dc()
    collector = DataCollector.__new__(DataCollector)
    collector.stats = {}
    collector._stats_lock = threading.Lock()
    for attr in ('predictive_maintenance', 'edge_decision', 'energy_manager',
                 'spc_analyzer', 'vibration_analyzer', 'oee_calculator',
                 'quality_predictor', 'anomaly_detector'):
        setattr(collector, attr, None)
    collector.device_control = dc
    collector._intel_enabled = lambda _name: True

    collector._dispatch_intelligence({
        'device_id': 'sensor_01',
        'register_name': 'temp',
        'value': 25.0,                    # 断线前的"正常"值
        'quality': GOOD_CODE,             # 质量码仍写着 GOOD（冻结快照）
        'timestamp': datetime.now() - timedelta(seconds=30),
    })

    started = _start(dc)

    assert started['success'] is False
    assert started['results']['press_01']['reason_code'] == 'DATA_STALE'
    assert manager.client.write_calls == []


# ---------------------------------------------------------------------------
# 3. 可信数据到达后保护恢复（不能一断线就永久锁死）
# ---------------------------------------------------------------------------

def test_fresh_good_sample_after_stale_restores_start():
    dc, manager = _dc()
    dc.check_interlocks('sensor_01', 'temp', 25.0,
                        quality=GOOD_CODE, updated_at=time.time() - 30)
    assert _start(dc)['success'] is False

    dc.check_interlocks('sensor_01', 'temp', 25.0,
                        quality=GOOD_CODE, updated_at=time.time())

    started = _start(dc)

    assert started['success'] is True, (
        '恢复可信采样后仍然拦着 —— 联锁会永久锁死设备')
    assert manager.client.write_calls, '恢复后应真正下发启动'


def test_trusted_good_sample_does_not_block():
    dc, _ = _dc()
    dc.check_interlocks('sensor_01', 'temp', 25.0,
                        quality=GOOD_CODE, updated_at=datetime.now())

    assert _start(dc)['success'] is True


# ---------------------------------------------------------------------------
# 4. 不可信数据不许触发停机动作，也不许解除已触发的联锁
# ---------------------------------------------------------------------------

def test_untrusted_sample_never_executes_stop_action():
    dc, manager = _dc([_block_rule(action='write_register')])
    # 先让它正常触发一次（写停机值）
    dc.check_interlocks('sensor_01', 'temp', 200.0,
                        quality=GOOD_CODE, updated_at=time.time())
    writes_after_trigger = len(manager.client.write_calls)
    assert writes_after_trigger >= 1, '前提不成立：可信超温应执行停机写入'

    dc.check_interlocks('sensor_01', 'temp', 200.0,
                        quality='BAD', updated_at=time.time() - 60)

    assert len(manager.client.write_calls) == writes_after_trigger, (
        '拿一条坏数据去执行/重复执行停机动作')
    assert dc._interlock_states['il_temp'] is True, (
        '不可信采样把已触发的联锁解除了')


def test_untrusted_sample_does_not_clear_state_via_other_logic():
    """坏值即使"低于阈值"，也不能当成条件解除。"""
    dc, _ = _dc()
    dc.check_interlocks('sensor_01', 'temp', 200.0,
                        quality=GOOD_CODE, updated_at=time.time())
    assert dc._interlock_states['il_temp'] is True

    dc.check_interlocks('sensor_01', 'temp', 0.0,
                        quality='BAD', updated_at=time.time())

    assert dc._interlock_states['il_temp'] is True
    status = dc.get_interlock_status()['il_temp']
    assert status['data_trusted'] is False
    assert status['data_issue']['reason_code'] == 'QUALITY_UNTRUSTED'
