# -*- coding: utf-8 -*-
"""round 302：全面审计后的六组「看起来做了 / 只做了一半」。

这一轮同时开了四路只读审计（报警状态机 / 展示层 API / 存储时序 / 智能层），
挑后果最重的六组一次修完。共同判据仍是那三条：
**内存与库是同一套真值；成功返回等于状态收敛；动作没执行就不许记成功。**
"""

from __future__ import annotations

import importlib
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. 声光消音：critical 级的蜂鸣器是 DO 常 ON，消音必须真的写 DO
# ---------------------------------------------------------------------------

def _bare_output(fail_channels=()):
    """构造一个不经 __init__ 的 AlarmOutput（硬件分支，可控哪些 DO 写失败）。"""
    from 报警层.alarm_output import AlarmOutput

    ao = AlarmOutput.__new__(AlarmOutput)
    ao._lock = threading.RLock()
    ao._io_lock = threading.RLock()
    ao._fail_streak = 0
    ao._circuit_open_until = None
    ao.simulation = False
    ao.do_red, ao.do_yellow, ao.do_green, ao.do_buzzer = 0, 1, 2, 3
    ao.current_state = {
        'red': True, 'yellow': False, 'green': False,
        'buzzer': True, 'pattern': None, 'level': 'critical',
        'message': 'x', 'since': None, 'acknowledged': False,
    }
    ao._buzzer_running = False
    ao._buzzer_thread = None
    ao._flash_running = False
    ao._flash_thread = None
    ao.config = {'modbus': {}}
    ao.enabled = True
    ao._write_do = lambda address, value: address not in fail_channels
    return ao


def test_acknowledge_writes_buzzer_off():
    """critical 的蜂鸣器是「DO 常 ON、不起脉冲线程」——只停线程等于没消音。"""
    ao = _bare_output()
    writes = []

    def rec(address, value):
        writes.append((address, value))
        return True

    ao._write_do = rec
    assert ao.acknowledge() is True

    assert (ao.do_buzzer, False) in writes, (
        f'消音没有写蜂鸣器 DO，现场会一直响到下一次 reset：writes={writes}')
    # 灯要保持当前状态（确认只消音，不清报警级别）
    assert (ao.do_red, True) in writes, '确认时把红灯也关掉了'


def test_acknowledge_reports_do_failure():
    """DO 写失败必须如实返回 False —— 否则界面说「已消音」、现场仍在响。"""
    ao = _bare_output(fail_channels={3})
    assert ao.acknowledge() is False, '蜂鸣器 DO 写失败时 acknowledge() 仍返回 True'


def test_manual_control_reports_do_failure():
    ao = _bare_output(fail_channels={0})
    result = ao.manual_control(red=True)
    assert result['success'] is False
    assert result['output_ok'] is False


def test_manual_control_success_is_reported():
    """反向对照：全部写成功时必须是 success。"""
    ao = _bare_output()
    result = ao.manual_control(red=True, buzzer=False)
    assert result['success'] is True and result['output_ok'] is True


# ---------------------------------------------------------------------------
# 2. 落库失败不许中断播报（round 282 的异常路径副本）
# ---------------------------------------------------------------------------

def _alarm_mgr(db=None):
    from 报警层.alarm_manager import AlarmManager
    return AlarmManager(db or MagicMock())


RULE = {'id': 'r1', 'name': '超温', 'level': 'critical',
        'threshold': 100, 'area': 'all'}


def test_persist_failure_does_not_stop_annunciation():
    """`insert_alarm` 抛异常时，声光/广播/推送必须照常 —— 记录与播报分离。"""
    db = MagicMock()
    db.insert_alarm.side_effect = RuntimeError('database is locked')
    mgr = _alarm_mgr(db)
    try:
        mgr.alarm_output = MagicMock()
        mgr.alarm_output.enabled = True
        mgr._should_emit_detail = lambda *a, **k: (True, '')
        mgr._record_emit = lambda *a, **k: None
        mgr._emit_websocket_alarm = MagicMock()

        mgr._trigger_alarm(RULE, 'd1', 'temp', 120.0, datetime.now())

        assert mgr.alarm_output.trigger_alarm.called, (
            '落库失败把声光也带走了 —— 内存已 FIRE、库没有、现场一声没响')
        assert mgr._output_errors.get('database', 0) == 1, '落库失败没有记账'
        payload = mgr._emit_websocket_alarm.call_args[0][0]
        assert payload.get('persisted') is False, (
            '推送里没标 persisted=False —— 前端会以为「弹了但历史查不到」是 bug')
    finally:
        mgr.stop()


def test_persist_success_marks_persisted_true():
    """反向对照：正常落库时 persisted 必须是 True。"""
    mgr = _alarm_mgr(MagicMock())
    try:
        mgr._should_emit_detail = lambda *a, **k: (True, '')
        mgr._record_emit = lambda *a, **k: None
        mgr._emit_websocket_alarm = MagicMock()

        mgr._trigger_alarm(RULE, 'd1', 'temp', 120.0, datetime.now())

        payload = mgr._emit_websocket_alarm.call_args[0][0]
        assert payload.get('persisted') is True
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# 3. 一条规则算崩不许带走同点位的其它规则
# ---------------------------------------------------------------------------

def test_one_bad_rule_does_not_stop_others():
    mgr = _alarm_mgr(MagicMock())
    try:
        base = {'device_id': 'd1', 'register_name': 't', 'condition': '>',
                'threshold': 1, 'level': 'warning'}
        mgr.rules = {
            'bad': {**base, 'id': 'bad', 'name': 'bad'},
            'good': {**base, 'id': 'good', 'name': 'good'},
        }
        mgr._rebuild_rules_index()

        seen = []
        real = mgr._process_alarm_state

        def spy(**kwargs):
            seen.append(kwargs['rule_id'])
            if kwargs['rule_id'] == 'bad':
                raise RuntimeError('boom')
            return real(**kwargs)

        mgr._process_alarm_state = spy
        mgr.check_alarm('d1', 't', 10.0, datetime.now())

        assert 'good' in seen, f'一条规则抛异常带走了后面的规则，实际处理了 {seen}'
        assert mgr._output_errors.get('alarm_engine', 0) >= 1, '规则异常没有记账'
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# 4. 旁路过期清理不许挂在「该点位有匹配规则」上
# ---------------------------------------------------------------------------

def test_expired_shelf_cleaned_without_matching_rules():
    """原来清理在 `if not matched_rules: return` 之后 —— 采集停了就永不过期。"""
    mgr = _alarm_mgr(MagicMock())
    try:
        mgr.shelve_alarm('r1', 'd1', 't', '维护', 'op', duration_minutes=1)
        mgr._shelved_alarms[('r1', 'd1', 't')].shelved_until = datetime(2020, 1, 1)
        mgr._last_shelf_cleanup = time.monotonic() - 1000

        # 一个**没有任何规则**的点位：旧实现在这里直接 return，清理永不执行
        mgr.check_alarm('unknown_dev', 'unknown_reg', 1.0, datetime.now())

        assert ('r1', 'd1', 't') not in mgr._shelved_alarms, (
            '没有匹配规则时旁路永不过期 —— 日志里却写着「时长: 1分钟」')
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# 5. 联锁：动作失败不置位、下轮重试；I/O 不许在状态锁内
# ---------------------------------------------------------------------------

IL_RULE = {
    'id': 'il1', 'name': '超温停机', 'priority': 0,
    'condition': {'type': 'threshold', 'device_id': 'd1',
                  'register': 'temp', 'operator': '>', 'value': 100},
    'action': {'type': 'write_register', 'device_id': 'd1',
               'register': 'run', 'value': 0},
}


def _dc():
    from 智能层.device_control import DeviceControlSafety
    dc = DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())
    dc._resolve_register_address = lambda device_id, register: 100
    return dc


def test_interlock_action_failure_keeps_state_and_retries():
    """动作抛异常后不许置「已触发」——否则停机动作、急停、审计永久跳过。"""
    dc = _dc()
    dc.add_interlock(dict(IL_RULE))
    client = MagicMock()
    client.write_single_register.side_effect = [RuntimeError('gateway down'), None]
    dc.device_manager.get_client.return_value = client

    dc.check_interlocks('d1', 'temp', 120.0)
    assert dc._interlock_states.get('il1') is False, (
        '动作没发出去，却已把联锁标成「已触发」—— 下一轮不会再试')
    assert dc._interlock_action_failures.get('il1') == 1

    dc.check_interlocks('d1', 'temp', 120.0)
    assert dc._interlock_states.get('il1') is True, '第二轮没有重试成功'
    assert 'il1' not in dc._interlock_action_failures, '重试成功后失败计数没清'


def test_interlock_status_not_blocked_by_slow_io():
    """状态锁不许跨 I/O：网关卡住时，联锁状态查询必须照常返回。"""
    dc = _dc()
    dc.add_interlock(dict(IL_RULE))
    client = MagicMock()

    def slow_write(address, value):
        time.sleep(1.2)

    client.write_single_register.side_effect = slow_write
    dc.device_manager.get_client.return_value = client

    worker = threading.Thread(target=lambda: dc.check_interlocks('d1', 'temp', 120.0),
                              daemon=True)
    worker.start()
    time.sleep(0.25)          # 让 check_interlocks 进到慢 I/O

    started = time.monotonic()
    dc.get_interlock_status()
    elapsed = time.monotonic() - started

    worker.join(timeout=5)
    assert elapsed < 0.5, (
        f'联锁状态查询被 Modbus I/O 卡住 {elapsed:.2f}s —— 采集线程正在等安全判定')


# ---------------------------------------------------------------------------
# 6. 模拟模式切换：运行时失败不许写盘；字符串 "false" 不许当 True
# ---------------------------------------------------------------------------

def _system_mod():
    return importlib.import_module('展示层.api.api_system')


def test_simulation_switch_rejection_does_not_persist(monkeypatch, client,
                                                       auth_headers, app):
    mod = _system_mod()
    app.device_manager.switch_simulation_mode.return_value = {
        'success': False, 'message': '真实模式下无法热切到模拟模式',
    }
    writes = []
    monkeypatch.setattr(mod, 'dump_yaml', lambda *a, **k: writes.append(1) or True)

    resp = client.post('/api/system/simulation-mode', headers=auth_headers,
                       json={'simulation_mode': True})

    assert resp.status_code == 400
    assert writes == [], (
        '运行时拒绝切换，配置却照样落盘 —— 下次重启会真的进模拟模式（假数据）')


def test_simulation_switch_string_false_is_false(monkeypatch, client,
                                                  auth_headers, app):
    """`bool("false") == True` —— 字符串必须显式解析。"""
    app.device_manager.switch_simulation_mode.return_value = {'success': True}
    mod = _system_mod()
    monkeypatch.setattr(mod, 'dump_yaml', lambda *a, **k: True)

    client.post('/api/system/simulation-mode', headers=auth_headers,
                json={'simulation_mode': 'false'})

    args = app.device_manager.switch_simulation_mode.call_args[0]
    assert args[0] is False, f'字符串 "false" 被当成了 {args[0]!r}'


def test_simulation_switch_rejects_non_boolean(client, auth_headers, app):
    resp = client.post('/api/system/simulation-mode', headers=auth_headers,
                       json={'simulation_mode': []})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 7. 设备增删：落盘失败要回滚
# ---------------------------------------------------------------------------

def test_add_device_rolls_back_when_save_fails(tmp_path, monkeypatch):
    from 采集层.device_manager import DeviceManager

    dm = DeviceManager(str(tmp_path / 'devices.yaml'), simulation_mode=True)
    monkeypatch.setattr(dm, '_save_config', lambda: False)

    ok = dm.add_device({'id': 'dev_x', 'name': 'X', 'protocol': 'modbus_tcp',
                        'host': '127.0.0.1', 'port': 502, 'enabled': False})

    assert ok is False, '配置没落盘却回了成功'
    assert 'dev_x' not in dm.devices, '落盘失败没回滚 —— 内存有、文件没有'


def test_remove_device_restores_when_save_fails(tmp_path, monkeypatch):
    from 采集层.device_manager import DeviceManager

    dm = DeviceManager(str(tmp_path / 'devices.yaml'), simulation_mode=True)
    dm.devices['dev_y'] = {'id': 'dev_y', 'name': 'Y', 'protocol': 'modbus_tcp',
                           'host': '127.0.0.1', 'port': 502, 'enabled': False}
    monkeypatch.setattr(dm, '_save_config', lambda: False)
    monkeypatch.setattr(dm, 'disconnect_device', lambda device_id: True)

    ok = dm.remove_device('dev_y')

    assert ok is False
    assert 'dev_y' in dm.devices, '落盘失败没把设备放回去 —— 重启后设备会「复活」'


def test_add_device_success_still_works(tmp_path, monkeypatch):
    """反向对照：落盘成功时照常添加。"""
    from 采集层.device_manager import DeviceManager

    dm = DeviceManager(str(tmp_path / 'devices.yaml'), simulation_mode=True)
    monkeypatch.setattr(dm, '_save_config', lambda: True)

    ok = dm.add_device({'id': 'dev_z', 'name': 'Z', 'protocol': 'modbus_tcp',
                        'host': '127.0.0.1', 'port': 502, 'enabled': False})

    assert ok is True
    assert 'dev_z' in dm.devices
