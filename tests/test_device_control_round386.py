# -*- coding: utf-8 -*-
"""round 386（一）：批量联锁归属 + 解除急停的恢复范围。

1. 批量启动时的联锁归属：
   · `emergency_stop` 联锁**一律全厂级**（它执行的就是停全厂）；
   · `and/or` 复合条件递归解析嵌套设备；
   · 解析不出设备（只给 key）→ 全厂级拦截，**绝不默认放行**。
3. 解除急停只恢复**本次急停实际停掉**的设备；原本已停的不动；联锁触发的跳过。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class _EchoClient:
    """写什么读回什么，且 `last_read_source == 'fresh'`。"""

    def __init__(self):
        self.connected = True
        self.last_read_source = 'fresh'
        self.coils: dict[int, bool] = {}
        self.registers: dict[int, int] = {}
        self.write_calls: list[tuple] = []
        self.fail_read = False
        self.fail_write = False

    def write_single_coil(self, address, value):
        self.write_calls.append(('coil', int(address), bool(value)))
        if self.fail_write:
            return False
        self.coils[int(address)] = bool(value)
        return True

    def write_single_register(self, address, value):
        self.write_calls.append(('register', int(address), int(value)))
        if self.fail_write:
            return False
        self.registers[int(address)] = int(value)
        return True

    def read_coils(self, address, count=1):
        if self.fail_read:
            raise OSError('模拟回读超时')
        return [self.coils.get(int(address), False)]

    def read_holding_registers(self, address, count=1):
        if self.fail_read:
            raise OSError('模拟回读超时')
        return [self.registers.get(int(address) + i, 0) for i in range(count)]


class _Manager:
    """可观测的假设备管理器（mechanical 类，带启停状态）。"""

    def __init__(self, devices: dict, stopped=(), client=None):
        self.devices = devices
        self.simulation_mode = False
        self.stopped = set(stopped)
        self.client = client or _EchoClient()
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.fail_start: set[str] = set()
        self.start_hook = None

    def get_all_devices(self):
        return self.devices

    def get_client(self, _device_id):
        return self.client

    def get_device_status(self, device_id):
        return {'device_id': device_id,
                'device_category': 'mechanical',
                'stopped': device_id in self.stopped}

    def stop_device(self, device_id, write=True):
        self.stop_calls.append(device_id)
        self.stopped.add(device_id)
        return True

    def start_device(self, device_id, write=True):
        self.start_calls.append(device_id)
        if self.start_hook is not None:
            self.start_hook(device_id)
        if device_id in self.fail_start:
            return False
        self.stopped.discard(device_id)
        return True

    def set_estop_override(self, active):
        pass


def _device_config():
    return {
        'protocol': 'modbus_tcp',
        'device_category': 'mechanical',
        'control': {
            'start': {'type': 'coil', 'address': 1, 'value': True},
            'stop': {'type': 'coil', 'address': 2, 'value': False},
            'reset': {'type': 'register', 'address': 3, 'value': 0},
        },
    }


def _devices(*ids):
    return {did: _device_config() for did in ids}


def _dc(manager):
    from 智能层.device_control import DeviceControlSafety
    return DeviceControlSafety(MagicMock(), manager, None,
                              config={'interlocks': []})


# ---------------------------------------------------------------------------
# 1. 联锁归属 / 批量启动
# ---------------------------------------------------------------------------

class TestInterlockAttribution:
    def test_emergency_stop_without_device_id_blocks_whole_batch(self):
        """`emergency_stop` 没绑 device_id 时**不能被归到条件设备**上放行其余设备。"""
        client = _EchoClient()
        dm = _Manager(_devices('plc_a', 'plc_b'), client=client)
        dc = _dc(dm)
        dc._interlock_rules = {'r_estop': {
            'id': 'r_estop', 'name': '超温急停',
            'condition': {'type': 'threshold', 'device_id': 'plc_a',
                          'register': 'temp', 'operator': '>', 'value': 1},
            'action': {'type': 'emergency_stop'},      # 未绑定设备
        }}
        dc._interlock_states = {'r_estop': True}

        res = dc.batch_control('start', 'op')

        assert res['success'] is False
        assert res['blocked_by'] == ['r_estop']
        assert client.write_calls == [], '急停级联锁被触发时仍向设备下发了写入'

    def test_emergency_stop_device_id_is_still_plant_wide(self):
        """动作写了 device_id 也一样是全厂级 —— 执行的是 `trigger_emergency_stop()`。"""
        client = _EchoClient()
        dm = _Manager(_devices('plc_a', 'plc_b'), client=client)
        dc = _dc(dm)
        dc._interlock_rules = {'r_estop': {
            'id': 'r_estop', 'name': '超温急停',
            'condition': {'type': 'threshold', 'device_id': 'plc_a',
                          'register': 'temp', 'operator': '>', 'value': 1},
            'action': {'type': 'emergency_stop', 'device_id': 'plc_a'},
        }}
        dc._interlock_states = {'r_estop': True}

        # 只启动"另一台"：旧逻辑 targets ∩ scope 为空 → 直接放行
        res = dc.batch_control('start', 'op', device_ids=['plc_b'])

        assert res['success'] is False
        assert 'r_estop' in res.get('blocked_by', [])
        assert client.write_calls == []

    def test_composite_condition_without_device_is_fail_closed(self):
        """`type: and` 且只给 key（点位名）→ 解析不出设备 → 按全厂级拦。"""
        dm = _Manager(_devices('plc_a', 'plc_b'))
        dc = _dc(dm)
        dc._interlock_rules = {'r_and': {
            'id': 'r_and', 'name': '复合条件',
            'condition': {'type': 'and', 'conditions': [
                {'key': 'temp', 'operator': '>', 'value': 1},
            ]},
            'action': {'type': 'block_start'},          # 也没绑设备
        }}
        dc._interlock_states = {'r_and': True}

        res = dc.batch_control('start', 'op')

        assert res['success'] is False, '解析不出归属的联锁被默认放行'
        assert res['blocked_by'] == ['r_and']
        assert dm.client.write_calls == []

    def test_composite_condition_blocks_only_nested_device(self):
        """复合条件里的嵌套设备要能被解析出来，只挡该设备、不连坐全厂。"""
        dm = _Manager(_devices('plc_a', 'plc_b'))
        dc = _dc(dm)
        dc._interlock_rules = {'r_and': {
            'id': 'r_and', 'name': 'A 超温禁止 A 启动',
            'condition': {'type': 'and', 'conditions': [
                {'type': 'threshold', 'device_id': 'plc_a',
                 'register': 'temp', 'operator': '>', 'value': 1},
                {'type': 'coil', 'device_id': 'plc_a',
                 'address': 9, 'value': True},
            ]},
            'action': {'type': 'block_start', 'device_id': 'plc_a'},
        }}
        dc._interlock_states = {'r_and': True}

        res = dc.batch_control('start', 'op')

        assert res['success'] is False, '被联锁覆盖的设备仍然启动了'
        assert res['results']['plc_a']['blocked_by'] == 'r_and'
        assert res['results']['plc_b']['success'] is True
        assert [c[1] for c in dm.client.write_calls if c[0] == 'coil'] == [1]

    def test_disabled_rule_with_stale_state_does_not_block(self):
        """已停用的规则即使残留 triggered=True，也不该拦住启动。"""
        dm = _Manager(_devices('plc_a'))
        dc = _dc(dm)
        dc._interlock_rules = {'r_off': {
            'id': 'r_off', 'name': '已停用',
            'enabled': False,
            'condition': {'type': 'threshold', 'device_id': 'plc_a',
                          'register': 't', 'operator': '>', 'value': 1},
            'action': {'type': 'block_start', 'device_id': 'plc_a'},
        }}
        dc._interlock_states = {'r_off': True}

        res = dc.batch_control('start', 'op')

        assert res['success'] is True



# ---------------------------------------------------------------------------
# 3. 解除急停只恢复"本次停掉的"
# ---------------------------------------------------------------------------

class TestEstopRestoreScope:
    def test_trigger_excludes_already_stopped_devices(self):
        dm = _Manager(_devices('m_run', 'm_maint'), stopped={'m_maint'})
        dc = _dc(dm)

        res = dc.trigger_emergency_stop('test')

        assert res['success'] is True
        assert res['already_stopped'] == ['m_maint']
        assert dc._estop_devices == ['m_run'], (
            '原本已停机的设备被记进了"急停停下的名单"')

    def test_reset_only_restarts_devices_stopped_by_estop(self):
        dm = _Manager(_devices('m_run', 'm_maint'), stopped={'m_maint'})
        dc = _dc(dm)
        dc.trigger_emergency_stop('test')
        dm.start_calls.clear()

        res = dc.reset_emergency_stop('eng')

        assert res['restarted_devices'] == ['m_run']
        assert dm.start_calls == ['m_run'], (
            f'检修停机的设备被自动启动了: {dm.start_calls}')

    def test_reset_reports_per_device_failure_and_continues(self):
        dm = _Manager(_devices('m1', 'm2', 'm3'))
        dc = _dc(dm)
        dc.trigger_emergency_stop('test')
        dm.fail_start = {'m2'}

        res = dc.reset_emergency_stop('eng')

        assert res['success'] is False
        assert res['restarted_devices'] == ['m1', 'm3']
        assert res['failed_devices'] == ['m2']
        assert res['failure_details'][0]['device_id'] == 'm2'
        assert res['failure_details'][0]['reason']

    def test_reset_skips_device_when_interlock_triggers_midway(self):
        """联锁在解除过程中触发 → 该设备跳过并记录原因，不影响其他台。"""
        dm = _Manager(_devices('m1', 'm2'))
        db = MagicMock()
        dc = _dc(dm)
        dc.database = db
        dc._interlock_rules = {'r1': {
            'id': 'r1', 'name': 'M2 超温',
            'condition': {'type': 'threshold', 'device_id': 'm2',
                          'register': 't', 'operator': '>', 'value': 1},
            'action': {'type': 'block_start', 'device_id': 'm2'},
        }}
        dc._interlock_states = {'r1': False}

        def _persist(_key, _payload):
            # 模拟时间窗：整体检查通过之后、逐台启动之前联锁触发
            dc._interlock_states['r1'] = True
            return True

        db.set_system_state.side_effect = _persist

        dc.trigger_emergency_stop('test')
        dc._interlock_states['r1'] = False     # 触发阶段不挡急停
        res = dc.reset_emergency_stop('eng')

        assert res['restarted_devices'] == ['m1']
        assert [d['device_id'] for d in res['skipped_devices']] == ['m2']
        assert 'r1' in res['skipped_devices'][0]['reason']
        assert res['success'] is False


