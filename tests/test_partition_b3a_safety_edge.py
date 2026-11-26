# -*- coding: utf-8 -*-
"""Partition B3a 回归测试：device_control / edge_decision / run.py 安全与边缘控制修复。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from 智能层.device_control import DeviceControlSafety
from 智能层.edge_decision import EdgeDecisionEngine


class _EchoClient:
    """支持寄存器与线圈读写的模拟客户端（可配置 16-bit 无符号回读）。"""

    def __init__(self, *, mask_uint16: bool = False):
        self.connected = True
        self.last_read_source = 'fresh'
        self.coils: dict[int, bool] = {}
        self.registers: dict[int, int] = {}
        self.write_calls: list[tuple] = []
        self.mask_uint16 = mask_uint16

    def write_single_coil(self, address, value):
        self.write_calls.append(('coil', int(address), bool(value)))
        self.coils[int(address)] = bool(value)
        return True

    def write_single_register(self, address, value):
        self.write_calls.append(('register', int(address), int(value)))
        stored = int(value) & 0xFFFF if self.mask_uint16 else int(value)
        self.registers[int(address)] = stored
        return True

    def read_coils(self, address, count=1):
        return [self.coils.get(int(address) + i, False) for i in range(count)]

    def read_holding_registers(self, address, count=1):
        return [self.registers.get(int(address) + i, 0) for i in range(count)]


class _FakeManager:
    """可控设备管理器测试桩。"""

    def __init__(self, devices: dict, *, simulation_mode: bool = False, client=None):
        self.devices = devices
        self.simulation_mode = simulation_mode
        self.client = client or _EchoClient()
        self.stopped: set[str] = set()
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.override_calls: list[bool] = []
        self.on_start = None

    def get_all_devices(self):
        return self.devices

    def get_client(self, _device_id):
        return self.client

    def get_device_status(self, device_id):
        return {
            'device_id': device_id,
            'device_category': self.devices.get(device_id, {}).get('device_category', 'mechanical'),
            'stopped': device_id in self.stopped,
        }

    def stop_device(self, device_id, write=True):
        self.stop_calls.append(device_id)
        self.stopped.add(device_id)
        return True

    def start_device(self, device_id, write=True):
        self.start_calls.append(device_id)
        if self.on_start is not None:
            self.on_start(device_id)
        self.stopped.discard(device_id)
        return True

    def set_estop_override(self, active):
        self.override_calls.append(bool(active))


def _mech_device(extra: dict | None = None) -> dict:
    cfg = {
        'protocol': 'modbus_tcp',
        'device_category': 'mechanical',
        'registers': [
            {'name': 'speed_sp', 'address': 10, 'data_type': 'uint16', 'access': 'rw'},
            {'name': 'trim_sp', 'address': 11, 'data_type': 'int16', 'access': 'rw', 'scale': 1.0, 'offset': 0.0},
            {'name': 'scaled_trim', 'address': 12, 'data_type': 'int16', 'access': 'rw', 'scale': 0.1, 'offset': 0.0},
        ],
        'write_limits': {'speed_sp': (0, 2000), 'trim_sp': (-100, 100), 'scaled_trim': (-50.0, 50.0)},
        'control': {
            'start': {'type': 'coil', 'address': 1, 'value': True},
            'stop': {'type': 'coil', 'address': 2, 'value': False},
        },
    }
    if extra:
        cfg.update(extra)
    return cfg


def _make_dc(tmp_path: Path, devices: dict | None = None, *, rules: list | None = None,
             simulation_mode: bool = False, client=None, max_data_age_seconds: float = 10.0):
    dm = _FakeManager(
        devices if devices is not None else {'m1': _mech_device()},
        simulation_mode=simulation_mode,
        client=client,
    )
    db = MagicMock()
    db.get_system_state.return_value = None
    db.set_system_state.return_value = True
    cfg = {'interlocks': rules if rules is not None else [], 'max_data_age_seconds': max_data_age_seconds}
    dc = DeviceControlSafety(db, dm, None, config=cfg)
    dc._audit_file = tmp_path / 'audit.jsonl'
    return dc, dm, db


# ===========================================================================
# 1. E-STOP reset 竞态（_estop_epoch + 慢重启保持急停 + get_estop_status 可见性）
# ===========================================================================

class TestEstopResetRaceAndVisibility:
    def test_estop_active_remains_true_until_slow_restart_finishes(self, tmp_path):
        dc, dm, _db = _make_dc(tmp_path, devices={'m1': _mech_device()})
        dc.trigger_emergency_stop('initial')
        assert dc._estop_epoch == 1

        observed_during_restart: dict[str, bool] = {}

        def _slow_start(did):
            observed_during_restart['active_attr'] = dc._estop_active
            observed_during_restart['status_active'] = dc.get_estop_status()['active']
            observed_during_restart['signal'] = dc._estop_signal.is_set()

        dm.on_start = _slow_start
        res = dc.reset_emergency_stop('eng1')

        assert res['success'] is True
        assert observed_during_restart == {
            'active_attr': True,
            'status_active': True,
            'signal': True,
        }
        assert dc._estop_active is False
        assert dc._estop_signal.is_set() is False
        assert dc._estop_devices == []
        assert dc.get_estop_status()['active'] is False

    def test_retriggered_estop_during_slow_reset_aborts_reset_and_keeps_estop(self, tmp_path):
        dc, dm, db = _make_dc(tmp_path, devices={'m1': _mech_device(), 'm2': _mech_device()})
        dc.trigger_emergency_stop('first_estop')
        assert dc._estop_epoch == 1

        retrigger_done = threading.Event()
        errors: list[BaseException] = []

        def _start_hook(did):
            if did == 'm1' and not retrigger_done.is_set():
                def _retrigger():
                    try:
                        dc.trigger_emergency_stop('concurrent_second_estop')
                    except BaseException as exc:  # pragma: no cover
                        errors.append(exc)
                    finally:
                        retrigger_done.set()

                t = threading.Thread(target=_retrigger, daemon=True)
                t.start()
                assert retrigger_done.wait(timeout=3.0)
                t.join(timeout=2.0)

        dm.on_start = _start_hook
        res = dc.reset_emergency_stop('eng1')

        assert not errors
        assert res['success'] is False
        assert res.get('reason_code') == 'ESTOP_RETRIGGERED'
        assert dc._estop_epoch == 2
        assert dc._estop_active is True
        assert dc._estop_signal.is_set() is True
        assert dc.get_estop_status()['active'] is True
        # m2 未在第二轮急停后继续被启动，且 m1 已被中止逻辑回停
        assert 'm2' not in dm.start_calls
        assert dm.override_calls[-1] is True
        # DB 最后一次持久化必须仍为 active=True
        last_payload = db.set_system_state.call_args.args[1]
        assert isinstance(last_payload, dict) and last_payload.get('active') is True

    def test_get_estop_status_active_reflects_signal_immediately(self, tmp_path):
        dc, _dm, _db = _make_dc(tmp_path)
        assert dc.get_estop_status()['active'] is False
        dc._estop_signal.set()
        assert dc._estop_active is False
        assert dc.get_estop_status()['active'] is True
        dc._estop_signal.clear()
        assert dc.get_estop_status()['active'] is False


# ===========================================================================
# 2. write_with_verification 持锁复核透传 origin
# ===========================================================================

class TestWriteRecheckPassesOrigin:
    def test_recheck_enforces_origin_when_disarmed_during_lock_wait(self, tmp_path, monkeypatch):
        dc, dm, _db = _make_dc(tmp_path, devices={'d1': _mech_device({'autonomy': 'supervised'})},
                               simulation_mode=True)
        ok, _ = dc.arm_autonomy('d1', 'supervisor', 10)
        assert ok is True

        orig_acquire = dc.acquire_device_lock

        def _acquire_and_disarm(device_id, operator, timeout=5.0):
            got = orig_acquire(device_id, operator, timeout=timeout)
            if got:
                dc.disarm_autonomy('d1', 'supervisor')
            return got

        monkeypatch.setattr(dc, 'acquire_device_lock', _acquire_and_disarm)
        res = dc.write_with_verification('d1', 10, 500, 'edge_decision', origin='auto')

        assert res['success'] is False
        assert res.get('rechecked') is True
        assert '未武装' in res['message']
        assert dm.client.write_calls == []

    def test_armed_auto_write_succeeds_positive_control(self, tmp_path):
        dc, dm, _db = _make_dc(tmp_path, devices={'d1': _mech_device({'autonomy': 'supervised'})},
                               simulation_mode=True)
        assert dc.arm_autonomy('d1', 'supervisor', 10)[0] is True
        res = dc.write_with_verification('d1', 10, 500, 'edge_decision', origin='auto')
        assert res['success'] is True
        assert ('register', 10, 500) in dm.client.write_calls


# ===========================================================================
# 3. 联锁 fail-closed（NaN+BAD、_interlock_last_trusted_mono、validate_write 全阻断）
# ===========================================================================

class TestInterlockFailClosed:
    @staticmethod
    def _block_rule(rid='il_temp', action_type='block_start', action_extra=None, cond_dev='sensor_01'):
        action = {'type': action_type}
        if action_type != 'emergency_stop':
            action['device_id'] = 'press_01'
        if action_extra:
            action.update(action_extra)
        return {
            'id': rid,
            'name': '温度联锁',
            'enabled': True,
            'priority': 1,
            'condition': {
                'type': 'threshold',
                'device_id': cond_dev,
                'register': 'temp',
                'operator': '>',
                'value': 120.0,
            },
            'action': action,
        }

    def test_nan_with_bad_quality_marks_untrusted_and_fails_closed(self, tmp_path):
        devices = {
            'sensor_01': {'protocol': 'modbus_tcp', 'device_category': 'instrument'},
            'press_01': _mech_device(),
        }
        dc, dm, _db = _make_dc(tmp_path, devices=devices, rules=[self._block_rule()])

        res = dc.check_interlocks('sensor_01', 'temp', float('nan'),
                                  quality='BAD', updated_at=time.time())
        assert isinstance(res, dict)
        assert res['trusted'] is False
        assert res['reason_code'] == 'QUALITY_UNTRUSTED'
        assert 'il_temp' in res['matched_rules']

        vw = dc.validate_write('press_01', 10, 100, 'op')
        assert vw['allowed'] is False
        assert vw['data_untrusted'] is True
        assert vw['reason_code'] == 'QUALITY_UNTRUSTED'

        bc = dc.batch_control('start', 'op', device_ids=['press_01'])
        assert bc['success'] is False
        assert dm.client.write_calls == []

    def test_zero_sample_startup_allowed_then_staleness_enforced(self, tmp_path):
        devices = {
            'sensor_01': {'protocol': 'modbus_tcp', 'device_category': 'instrument'},
            'press_01': _mech_device(),
        }
        dc, dm, _db = _make_dc(tmp_path, devices=devices, rules=[self._block_rule()],
                               max_data_age_seconds=0.05)

        # 0样本启动允许
        assert dc.validate_write('press_01', 10, 100, 'op')['allowed'] is True

        # 上报一条可信正常样本
        assert dc.check_interlocks('sensor_01', 'temp', 25.0,
                                   quality=192, updated_at=time.time()) is None
        assert dc.validate_write('press_01', 10, 100, 'op')['allowed'] is True

        # NaN+GOOD 不能刷新 _interlock_last_trusted_mono
        recorded = dc._interlock_last_trusted_mono['sensor_01']
        assert dc.check_interlocks('sensor_01', 'temp', float('nan'),
                                   quality=192, updated_at=time.time()) is None
        assert dc._interlock_last_trusted_mono['sensor_01'] == recorded

        # 超过 max_data_age_seconds 无新采样 → DATA_STALE fail-closed
        time.sleep(0.08)
        vw_stale = dc.validate_write('press_01', 10, 100, 'op')
        assert vw_stale['allowed'] is False
        assert vw_stale['reason_code'] == 'DATA_STALE'
        assert vw_stale['data_untrusted'] is True

        bc_stale = dc.batch_control('start', 'op', device_ids=['press_01'])
        assert bc_stale['success'] is False
        assert bc_stale['results']['press_01']['reason_code'] == 'DATA_STALE'
        assert dm.client.write_calls == []

        # 恢复可信新鲜样本后解除拦截
        assert dc.check_interlocks('sensor_01', 'temp', 25.0,
                                   quality=192, updated_at=time.time()) is None
        assert dc.validate_write('press_01', 10, 100, 'op')['allowed'] is True

    def test_validate_write_blocks_write_register_and_emergency_stop_interlocks(self, tmp_path):
        devices = {
            'sensor_01': {'protocol': 'modbus_tcp', 'device_category': 'instrument'},
            'press_01': _mech_device(),
            'press_02': _mech_device(),
        }
        wr_rule = self._block_rule(
            rid='il_wr',
            action_type='write_register',
            action_extra={'device_id': 'press_01', 'register': 'speed_sp', 'value': 0},
        )
        dc, _dm, _db = _make_dc(tmp_path, devices=devices, rules=[wr_rule])
        dc._interlock_states['il_wr'] = True

        assert dc.validate_write('press_01', 10, 500, 'op')['allowed'] is False
        # 另一台无关设备不受局部 write_register 联锁阻断
        assert dc.validate_write('press_02', 10, 500, 'op')['allowed'] is True

        # emergency_stop（即使 action 指定了某个 device_id 或未指定）均全厂级阻断
        estop_rule = self._block_rule(rid='il_es', action_type='emergency_stop',
                                      action_extra={'device_id': 'press_01'})
        dc2, _dm2, _db2 = _make_dc(tmp_path, devices=devices, rules=[estop_rule])
        dc2._interlock_states['il_es'] = True
        assert dc2.validate_write('press_01', 10, 500, 'op')['allowed'] is False
        assert dc2.validate_write('press_02', 10, 500, 'op')['allowed'] is False

        # 被旁路后应放行
        with dc2._lock:
            dc2._interlock_bypass.add('il_es')
        assert dc2.validate_write('press_02', 10, 500, 'op')['allowed'] is True


# ===========================================================================
# 4. _bypass_lock 保护 _active_bypasses & 关闭 removed 规则的 approved 请求
# ===========================================================================

class TestBypassLockAndCleanup:
    def test_remove_interlock_closes_approved_requests_and_clears_active(self, tmp_path):
        rule = {
            'id': 'r1',
            'name': '超温',
            'condition': {'type': 'threshold', 'device_id': 'm1', 'register': 't', 'operator': '>', 'value': 100},
            'action': {'type': 'block_start', 'device_id': 'm1'},
        }
        dc, _dm, db = _make_dc(tmp_path, rules=[rule])
        rid = dc.request_bypass('r1', 'alice', '检修', timeout_minutes=30)
        assert dc.approve_bypass(rid, 'bob')[0] is True
        assert dc.approve_bypass(rid, 'carol')[0] is True
        assert 'r1' in dc._active_bypasses
        assert dc._bypass_requests[rid].status == 'approved'

        try:
            assert dc.remove_interlock('r1') is True
            assert 'r1' not in dc._active_bypasses
            assert 'r1' not in dc._interlock_bypass
            assert 'r1' not in dc._bypass_timers
            assert dc._bypass_requests[rid].status != 'approved'
            assert db.set_system_state.call_args.args[1] is None
        finally:
            dc._cancel_bypass_timer('r1')

    def test_restore_interlock_closes_approved_requests(self, tmp_path):
        rule = {
            'id': 'r1',
            'name': '超温',
            'condition': {'type': 'threshold', 'device_id': 'm1', 'register': 't', 'operator': '>', 'value': 100},
            'action': {'type': 'block_start', 'device_id': 'm1'},
        }
        dc, _dm, _db = _make_dc(tmp_path, rules=[rule])
        rid = dc.request_bypass('r1', 'alice', '检修', timeout_minutes=30)
        dc.approve_bypass(rid, 'bob')
        dc.approve_bypass(rid, 'carol')

        try:
            assert dc.restore_interlock('r1', 'eng') is True
            assert 'r1' not in dc._active_bypasses
            assert dc._bypass_requests[rid].status != 'approved'
        finally:
            dc._cancel_bypass_timer('r1')


# ===========================================================================
# 5. 有符号负 int16 写入与无符号保持寄存器回读比对
# ===========================================================================

class TestInt16SignedReadback:
    def test_int16_negative_raw_and_engineering_match_unsigned_65531(self, tmp_path):
        client = _EchoClient(mask_uint16=True)
        dc, _dm, _db = _make_dc(tmp_path, devices={'m1': _mech_device()}, simulation_mode=True, client=client)

        # 直接写原始 -5 → 寄存器返回 65531
        res_raw = dc.write_with_verification('m1', 11, -5, 'op', engineering=False)
        assert res_raw['success'] is True
        assert res_raw['verified'] is True
        assert client.registers[11] == 65531

        # 工程值 -0.5 (scale=0.1 → raw=-5) → 寄存器返回 65531
        res_eng = dc.write_with_verification('m1', 12, -0.5, 'op', engineering=True)
        assert res_eng['success'] is True
        assert res_eng['verified'] is True
        assert client.registers[12] == 65531

    def test_int16_mismatch_and_uint16_negative_still_fail(self, tmp_path):
        client = _EchoClient(mask_uint16=True)
        dc, _dm, _db = _make_dc(tmp_path, devices={'m1': _mech_device()}, simulation_mode=True, client=client)

        # 回读不一致（强行改写客户端读回为 65530 = -6）
        orig_read = client.read_holding_registers
        client.read_holding_registers = lambda addr, cnt=1: [65530]
        res_mismatch = dc.write_with_verification('m1', 11, -5, 'op', engineering=False)
        assert res_mismatch['success'] is False
        assert res_mismatch['verified'] is False
        client.read_holding_registers = orig_read

        # uint16 不把 -5 当成 65531 放行
        dc._write_limits['m1']['speed_sp'] = (-100, 2000)
        res_u16 = dc.write_with_verification('m1', 10, -5, 'op', engineering=False)
        assert res_u16['success'] is False
        assert res_u16['verified'] is False


# ===========================================================================
# 6 & 7. trigger_emergency_stop 声光/广播异常兜底 & validate_write 警告锁保护
# ===========================================================================

class TestEstopAlarmExceptionAndWarningsLock:
    def test_trigger_estop_completes_audit_and_return_when_alarm_broadcast_raise(self, tmp_path, caplog):
        dc, _dm, _db = _make_dc(tmp_path)
        am = MagicMock()
        am.alarm_output.trigger_alarm.side_effect = RuntimeError('DO offline')
        am.broadcast_system.speak.side_effect = OSError('TTS offline')
        dc.alarm_manager = am

        with caplog.at_level(logging.WARNING):
            res = dc.trigger_emergency_stop('alarm_fault_test')

        assert res['success'] is True
        assert dc._estop_active is True
        assert dc._estop_signal.is_set() is True
        assert any(entry['action'] == 'emergency_stop' for entry in dc.get_audit_log())
        assert any('声光报警触发失败' in r.getMessage() for r in caplog.records)
        assert any('广播播报失败' in r.getMessage() for r in caplog.records)

    def test_validate_write_warnings_concurrent_mutation_no_runtime_error(self, tmp_path):
        rule = {
            'id': 'r_warn',
            'name': '警告测试',
            'condition': {'type': 'threshold', 'device_id': 'other', 'register': 't', 'operator': '>', 'value': 10},
            'action': {'type': 'block_start', 'device_id': 'other'},
        }
        dc, _dm, _db = _make_dc(tmp_path, rules=[rule], simulation_mode=True)
        stop = threading.Event()
        errors: list[BaseException] = []

        def _mutator():
            try:
                i = 0
                while not stop.is_set():
                    with dc._lock:
                        rid = f'r_dyn_{i % 5}'
                        dc._interlock_rules[rid] = dict(rule, id=rid)
                        dc._interlock_states[rid] = True
                        dc._interlock_bypass.add(rid)
                        dc._interlock_bypass.discard(rid)
                        dc._interlock_rules.pop(rid, None)
                        dc._interlock_states.pop(rid, None)
                    i += 1
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        t = threading.Thread(target=_mutator, daemon=True)
        t.start()
        try:
            for _ in range(100):
                res = dc.validate_write('m1', 10, 50, 'op')
                assert isinstance(res, dict)
        finally:
            stop.set()
            t.join(timeout=3.0)
        assert not errors


# ===========================================================================
# 8. run.py edge_write_register & EdgeDecisionEngine 安全/并发/清理
# ===========================================================================

class TestRunEdgeWriteAndEdgeDecision:
    def test_edge_write_register_resolves_name_and_returns_strict_bool(self, tmp_path):
        import run

        dc, dm, _db = _make_dc(tmp_path, devices={'m1': _mech_device({'autonomy': 'automatic'})},
                               simulation_mode=True)

        # 按点位名称 'speed_sp' 解析地址 10 并成功写入
        assert run._execute_edge_write_register(dm, dc, 'm1', 'speed_sp', 350.0) is True
        assert dm.client.registers[10] == 350

        # 字符串数字地址也能正常解析
        assert run._execute_edge_write_register(dm, dc, 'm1', '10', 400.0) is True
        assert dm.client.registers[10] == 400

        # 未知点位名 / 布尔地址 → False（绝不返回 None）
        assert run._execute_edge_write_register(dm, dc, 'm1', 'nonexistent_reg', 100.0) is False
        assert run._execute_edge_write_register(dm, dc, 'm1', True, 100.0) is False

        # HITL pending_approval → False（不是 None）
        dc.set_autonomy_mode('m1', 'manual', 'op')
        assert run._execute_edge_write_register(dm, dc, 'm1', 'speed_sp', 500.0) is False

        # 急停阻断 → False
        dc.set_autonomy_mode('m1', 'automatic', 'op')
        dc.trigger_emergency_stop('estop')
        assert run._execute_edge_write_register(dm, dc, 'm1', 'speed_sp', 500.0) is False

        # 异常 → False
        bad_dc = MagicMock()
        bad_dc.write_with_verification.side_effect = RuntimeError('bus error')
        assert run._execute_edge_write_register(dm, bad_dc, 'm1', 'speed_sp', 500.0) is False

        # 端到端接入 EdgeDecisionEngine：写入被拒时 action_failures 递增且不记成功
        eng = EdgeDecisionEngine(MagicMock())
        eng.register_action('write_register', lambda d, r, v: run._execute_edge_write_register(dm, dc, d, r, v))
        eng.add_rule('r_wr', condition={'type': 'threshold', 'key': 'm1:t', 'operator': 'gt', 'value': 10},
                     action={'type': 'write_register', 'target': 'm1:speed_sp', 'value': 200})
        eng.update_data('m1:t', 50.0, quality=192)
        eng._execute_cycle()
        assert eng.get_status()['action_failures'] == 1
        assert eng.rules['r_wr']['trigger_count'] == 0
        assert eng.get_decision_log(1)[0]['ok'] is False

        # set_alarm 返回 None 仍按成功处理
        eng.register_action('set_alarm', lambda msg, lvl: None)
        eng.add_rule('r_alm', condition={'type': 'threshold', 'key': 'm1:t', 'operator': 'gt', 'value': 10},
                     action={'type': 'set_alarm', 'message': 'warn'})
        eng._execute_cycle()
        assert eng.rules['r_alm']['trigger_count'] == 1

    def test_edge_decision_fresh_snapshot_quality_staleness_and_nonfinite_bool(self):
        eng = EdgeDecisionEngine(MagicMock(), {'max_data_age_seconds': 5.0})

        # 数值 192 与遗留 2-arg 省略 meta 均放行
        eng.update_data('d1:good_num', 42.0, quality=192)
        eng.update_data('d1:legacy_2arg', 43.0)
        eng._data_snapshot['d1:direct_no_meta'] = 44.0

        # 非 192 质量码、超龄（monotonic 与 wall clock）、非有限与 bool 拒绝
        eng.update_data('d1:uncertain', 10.0, quality=64)
        eng.update_data('d1:bad_code', 10.0, quality=0)
        eng.update_data('d1:bad_str', 10.0, quality='BAD')
        eng.update_data('d1:none_q', 10.0, quality=None)
        eng.update_data('d1:bool_q', 10.0, quality=True)
        eng.update_data('d1:stale_mono', 10.0, quality=192, ts=time.monotonic() - 20.0)
        eng.update_data('d1:stale_wall', 10.0, quality=192, ts=time.time() - 20.0)
        eng.update_data('d1:stale_dt', 10.0, quality=192, ts=datetime.now() - timedelta(seconds=20))
        eng.update_data('d1:bool_val', True, quality=192)
        eng.update_data('d1:nan_val', float('nan'), quality=192)

        snap = eng._fresh_snapshot()
        assert snap == {'d1:good_num': 42.0, 'd1:legacy_2arg': 43.0, 'd1:direct_no_meta': 44.0}

    def test_edge_decision_forget_device_and_run_py_wiring(self):
        eng = EdgeDecisionEngine(MagicMock())
        eng.update_data('d1:temp', 80.0, quality=192)
        eng.update_data('d1:press', 1.2, quality=192)
        eng.update_data('d10:temp', 90.0, quality=192)

        assert eng.forget_device('d1') is True
        assert 'd1:temp' not in eng.get_data_snapshot()
        assert 'd1:press' not in eng.get_data_snapshot()
        assert 'd1:temp' not in eng._data_quality
        assert 'd10:temp' in eng.get_data_snapshot()
        assert eng.forget_device('d1') is False

        run_src = (Path(__file__).resolve().parents[1] / 'run.py').read_text(encoding='utf-8')
        start = run_src.find('def _forget_device_state')
        assert start > 0
        block = run_src[start:start + 900]
        assert 'edge_decision' in block

    def test_edge_decision_log_zero_and_rule_type_disambiguation(self):
        eng = EdgeDecisionEngine(MagicMock())
        eng.register_action('set_alarm', lambda msg, lvl: None)
        eng.add_rule('dup', condition={'type': 'threshold', 'key': 'd1:t', 'operator': 'gt', 'value': 10},
                     action={'type': 'set_alarm', 'message': 'rule'})
        eng.add_interlock('dup', condition={'type': 'threshold', 'key': 'd1:t', 'operator': 'gt', 'value': 10},
                          action={'type': 'set_alarm', 'message': 'interlock'})

        eng._execute_action({'type': 'set_alarm', 'message': 'il'}, 'dup', 'interlock', {})
        assert eng.interlocks['dup']['trigger_count'] == 1
        assert eng.rules['dup']['trigger_count'] == 0

        eng._execute_action({'type': 'set_alarm', 'message': 'r'}, 'dup', 'rule', {})
        assert eng.rules['dup']['trigger_count'] == 1
        assert eng.interlocks['dup']['trigger_count'] == 1

        assert eng.get_decision_log(0) == []
        assert eng.get_decision_log(-5) == []
        assert len(eng.get_decision_log(1)) == 1
        assert len(eng.get_decision_log(10)) == 2

    def test_edge_decision_concurrent_callbacks_log_and_status(self):
        eng = EdgeDecisionEngine(MagicMock())
        eng.register_action('set_alarm', lambda msg, lvl: None)
        eng.add_rule('r1', condition={'type': 'threshold', 'key': 'd1:t', 'operator': 'gt', 'value': 10},
                     action={'type': 'set_alarm', 'message': 'hi'})
        eng.update_data('d1:t', 50.0, quality=192)

        stop = threading.Event()
        errors: list[BaseException] = []

        def _writer():
            try:
                i = 0
                while not stop.is_set():
                    eng.register_action(f'cb_{i % 10}', lambda *_a: None)
                    eng._execute_cycle()
                    i += 1
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def _reader():
            try:
                while not stop.is_set():
                    _ = eng.get_status()
                    _ = eng.get_decision_log(10)
                    _ = eng.get_decision_log(0)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        t1 = threading.Thread(target=_writer, daemon=True)
        t2 = threading.Thread(target=_reader, daemon=True)
        t1.start()
        t2.start()
        try:
            time.sleep(0.25)
        finally:
            stop.set()
            t1.join(timeout=3.0)
            t2.join(timeout=3.0)

        assert not errors
