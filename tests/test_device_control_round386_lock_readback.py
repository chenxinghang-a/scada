# -*- coding: utf-8 -*-
"""round 386（二）：批量写/同步标志的临界区 + 回读验证语义。

2. 「写点位 + 同步启停标志 + 审计」同一把设备锁；同步失败要回滚或标记不一致。
4. 回读未验证（读不到/读缓存）不得报成功，`effective` 只在真正验证通过时为 True。
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
# 2. 写入 + 同步标志同一临界区
# ---------------------------------------------------------------------------

class TestBatchStateSyncUnderDeviceLock:
    def test_state_sync_runs_while_device_lock_is_held(self):
        """`start_device(write=False)` 必须在设备锁内执行。"""
        dm = _Manager(_devices('plc_a'))
        dc = _dc(dm)
        observed = {}

        def _hook(device_id):
            lock = dc._device_locks.get(device_id)
            observed['locked'] = bool(lock and lock.locked())

        dm.start_hook = _hook
        res = dc.batch_control('start', 'op')

        assert res['success'] is True
        assert observed.get('locked') is True, (
            '状态同步发生在设备锁之外 —— 与写点位不是同一临界区')
        # 临界区结束后锁必须释放
        assert dc._device_locks['plc_a'].locked() is False

    def test_start_sync_failure_rolls_back_to_stop(self):
        """启动写入成功、标志同步失败 → 回写停止点位（回到安全侧）。"""
        client = _EchoClient()
        dm = _Manager(_devices('plc_a'), client=client)
        dm.fail_start = {'plc_a'}
        dc = _dc(dm)

        res = dc.batch_control('start', 'op')

        result = res['results']['plc_a']
        assert result['success'] is False
        assert result['rolled_back'] is True
        assert result['state_inconsistent'] is False
        assert ('coil', 1, True) in client.write_calls, '启动点位没有下发'
        assert ('coil', 2, False) in client.write_calls, (
            '状态同步失败后没有回写停止点位 —— 设备可能在转、系统认为已停')
        assert dc._device_locks['plc_a'].locked() is False

    def test_stop_sync_failure_is_marked_inconsistent_and_not_restarted(self):
        """停机标志同步失败 → 标记不一致，**不得**为了状态一致把设备重新启动。"""
        client = _EchoClient()
        dm = _Manager(_devices('plc_a'), client=client)
        dm.fail_start = {'plc_a'}       # stop 动作走 stop_device，这里让它失败
        dm.stop_device = lambda device_id, write=True: False
        dc = _dc(dm)

        res = dc.batch_control('stop', 'op')

        result = res['results']['plc_a']
        assert result['success'] is False
        assert result['state_inconsistent'] is True
        assert ('coil', 1, True) not in client.write_calls, (
            '停机状态同步失败后竟然回写了启动点位 —— 设备会被误启动')

    def test_lock_is_released_when_write_fails(self):
        client = _EchoClient()
        client.fail_write = True
        dm = _Manager(_devices('plc_a'), client=client)
        dc = _dc(dm)

        res = dc.batch_control('start', 'op')

        assert res['success'] is False
        assert dc._device_locks['plc_a'].locked() is False



# ---------------------------------------------------------------------------
# 4. 回读未验证不得报成功
# ---------------------------------------------------------------------------

class TestReadbackVerification:
    def _dc(self, client):
        dm = MagicMock()
        dm.simulation_mode = True
        dm.devices = {}
        dm.get_client = lambda _did: client
        from 智能层.device_control import DeviceControlSafety
        return DeviceControlSafety(MagicMock(), dm, None,
                                   config={'interlocks': []})

    def test_verified_match_reports_success(self):
        client = _EchoClient()
        dc = self._dc(client)
        res = dc.write_with_verification('d1', 1, 100, 'op',
                                         kind='register', control_point=True)
        assert res['success'] is True and res['effective'] is True
        assert res['verified'] is True

    def test_read_timeout_is_failure_with_reason(self):
        client = _EchoClient()
        client.fail_read = True
        dc = self._dc(client)
        res = dc.write_with_verification('d1', 1, 100, 'op',
                                         kind='register', control_point=True)

        assert res['success'] is False, '回读超时仍报成功'
        assert res['effective'] is False
        assert res['verified'] is None
        assert '未验证' in res['message'] and '模拟回读超时' in res['message']
        assert res['unverified_reason']

    def test_stale_cache_is_failure_with_reason(self):
        client = _EchoClient()
        client.last_read_source = 'cache'
        dc = self._dc(client)
        res = dc.write_with_verification('d1', 1, 100, 'op',
                                         kind='register', control_point=True)

        assert res['success'] is False, '陈旧缓存回读被当成验证通过'
        assert res['effective'] is False
        assert '陈旧缓存' in res['message']

    def test_unsupported_readback_is_failure(self):
        class _WriteOnlyClient:
            connected = True
            last_read_source = 'fresh'

            def write_single_register(self, address, value):
                return True

        res = self._dc(_WriteOnlyClient()).write_with_verification(
            'd1', 1, 7, 'op', kind='register', control_point=True)

        assert res['success'] is False
        assert res['effective'] is False
        assert '不支持' in res['message']
