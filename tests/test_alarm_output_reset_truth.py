# -*- coding: utf-8 -*-
"""声光报警「复位」的假成功回归测试（round 218）。

缺陷
----
`报警层/alarm_output.py::_write_all_do` 丢掉四次 `_write_do` 的返回值，
`reset()` 无条件 `return True` 并打印「报警输出已复位（绿灯正常）」；
`报警层/alarm_manager.py::reset_alarm` 又丢掉 `_call_output_method(...'reset')`
的结果、同样无条件 `return True`；API 于是回「报警输出已复位（绿灯正常）」。

净效果：**Modbus DO 写失败时（串口断开 / 从站无响应 / 线圈地址配错），
界面显示已复位、现场红灯和蜂鸣器还在响**。

声光报警是这个系统的安全出口 —— 这种假成功比「没有这个功能」更危险：
操作员会据此判断「现场已恢复正常」并离开。

本文件钉四层：
  ① `_write_all_do` 任一通道失败 → False；
  ② `AlarmOutput.reset()` 失败时返回 False，且**不许**再打印「已复位」的成功日志；
  ③ `AlarmManager.reset_alarm()` 把输出失败透传出去（不再恒 True）；
  ④ `POST /api/alarm-output/reset` 回 500 + `output_ok: false`。
"""

import logging
import threading

import pytest


# ---------------------------------------------------------------------------
# ① / ② AlarmOutput 层
# ---------------------------------------------------------------------------

def _bare_output(fail_channels=()):
    """构造一个不经 __init__ 的 AlarmOutput（硬件模式，可控哪些 DO 写失败）。"""
    from 报警层.alarm_output import AlarmOutput

    ao = AlarmOutput.__new__(AlarmOutput)
    ao._lock = threading.RLock()
    # round 286：I/O 改成用独立的 `_io_lock`（状态锁不再跨网络调用持有），
    # 以及 DO 通道的熔断状态。裸对象必须把这些字段也备齐。
    ao._io_lock = threading.RLock()
    ao._fail_streak = 0
    ao._circuit_open_until = None
    ao.simulation = False          # 走硬件分支，_write_do 才会真的「失败」
    ao.do_red, ao.do_yellow, ao.do_green, ao.do_buzzer = 0, 1, 2, 3
    ao.current_state = {
        'red': True, 'yellow': False, 'green': False,
        'buzzer': True, 'pattern': None, 'level': 'high',
        'message': 'x', 'since': None, 'acknowledged': False,
    }
    ao._buzzer_running = False
    ao._buzzer_thread = None
    ao._flash_running = False
    ao._flash_thread = None
    ao.config = {'modbus': {}}
    ao.enabled = True

    def fake_write_do(address, value):
        return address not in fail_channels

    ao._write_do = fake_write_do
    return ao


def test_write_all_do_true_when_all_succeed():
    ao = _bare_output()
    assert ao._write_all_do(red=False, yellow=False, green=True, buzzer=False) is True


def test_write_all_do_false_when_any_channel_fails():
    ao = _bare_output(fail_channels={2})  # 绿灯通道写失败
    assert ao._write_all_do(red=False, yellow=False, green=True, buzzer=False) is False, (
        "有通道写失败时 _write_all_do 仍返回真值 —— 复位失败会被当成功"
    )


def test_reset_returns_false_on_write_failure(caplog):
    ao = _bare_output(fail_channels={0, 3})  # 红灯 + 蜂鸣器都没关掉
    with caplog.at_level(logging.INFO, logger='报警层.alarm_output'):
        ok = ao.reset()

    assert ok is False, "DO 写失败时 reset() 仍返回 True"
    messages = '\n'.join(r.getMessage() for r in caplog.records)
    assert '报警输出已复位（绿灯正常）' not in messages, (
        "写失败时仍打印了「已复位（绿灯正常）」—— 日志也在说谎"
    )
    assert 'DO 写入失败' in messages or '仍处于报警态' in messages, (
        f"没有留下可诊断的失败日志，实际日志: {messages!r}"
    )


def test_reset_returns_true_on_success(caplog):
    ao = _bare_output()
    with caplog.at_level(logging.INFO, logger='报警层.alarm_output'):
        assert ao.reset() is True
    assert '报警输出已复位（绿灯正常）' in '\n'.join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# ②b 触发路径：报警响了但声光没动，也必须显式失败
# ---------------------------------------------------------------------------

def test_trigger_alarm_returns_false_when_do_write_fails():
    """`trigger_alarm` 写 DO 失败时必须返回 False。

    `AlarmManager._call_output_method` 的契约是「显式 False 才算失败」；
    旧实现无论写没写成功都返回日志文案（字符串），于是**报警触发了、
    声光没动，系统却记成输出成功**，errors 计数也不涨。
    """
    ao = _bare_output(fail_channels={0, 3})   # 红灯 + 蜂鸣器都写不进去
    result = ao.trigger_alarm('critical', '压力超限', 'dev1')
    assert result is False, (
        f"DO 写失败时 trigger_alarm 返回了 {result!r} —— 上层会当成输出成功"
    )


def test_trigger_alarm_returns_message_on_success():
    ao = _bare_output()
    result = ao.trigger_alarm('critical', '压力超限', 'dev1')
    assert isinstance(result, str) and '严重报警' in result, (
        f"正常路径应返回日志文案，实际 {result!r}"
    )


def test_trigger_alarm_failure_is_counted_by_manager():
    """端到端：DO 失败 → `_call_output_method` 记一次 alarm_output 失败。"""
    mgr = _bare_alarm_manager(_bare_output(fail_channels={0}))
    ok = mgr._call_output_method('alarm_output', mgr.alarm_output, 'trigger_alarm',
                                 level='critical', message='x', device_id='d1')
    assert ok is False
    assert mgr._get_output_error_counts().get('alarm_output', 0) == 1


def test_trigger_alarm_dedup_branch_stays_neutral():
    """同级去重是**正常分支**，不能因为「没重新点灯」就被记成失败。"""
    ao = _bare_output()
    ao.trigger_alarm('critical', 'first', 'd1')
    # 第二次同级别、未确认 → 只更新消息、不动灯，返回 None
    assert ao.trigger_alarm('critical', 'second', 'd1') is None


# ---------------------------------------------------------------------------
# ③ AlarmManager 层：失败要透传
# ---------------------------------------------------------------------------

def _bare_alarm_manager(output):
    from 报警层.alarm_manager import AlarmManager

    mgr = AlarmManager.__new__(AlarmManager)
    mgr._state_lock = threading.RLock()
    mgr.alarm_states = {('r1', 'dev1', 'temp'): {'alarm_id': 'r1', 'level': 'high'}}
    mgr._escalation_manager = None
    mgr._dedup_lock = threading.Lock()
    mgr._emit_history = {}
    mgr._acknowledge_history = {}
    mgr.alarm_output = output
    mgr._output_errors = {}
    mgr._output_error_lock = threading.Lock()
    # round 267：连续失败计数与"通道已报警"集合（`_record_output_failure` 要用）
    mgr._output_consecutive = {}
    mgr._output_broken = set()
    return mgr


def test_reset_alarm_propagates_output_failure():
    mgr = _bare_alarm_manager(_bare_output(fail_channels={0}))
    assert mgr.reset_alarm() is False, (
        "声光复位失败，reset_alarm() 仍返回 True —— 上层无法告知操作员"
    )
    assert mgr.alarm_states == {}, "软件侧状态仍应被清空（不要因为硬件失败就不复位）"


def test_reset_alarm_true_when_output_ok():
    mgr = _bare_alarm_manager(_bare_output())
    assert mgr.reset_alarm() is True


def test_reset_alarm_true_when_no_output_configured():
    """没配声光输出时，「复位」的语义只有清状态，应视为成功。"""
    mgr = _bare_alarm_manager(None)
    assert mgr.reset_alarm() is True


def test_reset_alarm_records_output_failure_count():
    mgr = _bare_alarm_manager(_bare_output(fail_channels={3}))
    mgr.reset_alarm()
    assert mgr._get_output_error_counts().get('alarm_output', 0) >= 1, (
        "输出失败没有被计入可观测的 errors 计数"
    )


# ---------------------------------------------------------------------------
# ④ API 层
# ---------------------------------------------------------------------------

def test_api_reset_reports_hardware_failure(client, auth_headers, app):
    app.alarm_manager.alarm_output = object()      # 非 None 即「已配置」
    app.alarm_manager.reset_alarm.return_value = False

    resp = client.post('/api/alarm-output/reset', headers=auth_headers)

    assert resp.status_code == 500, (
        f"硬件复位失败却返回 {resp.status_code} —— 界面会显示已恢复正常"
    )
    body = resp.get_json()
    assert body['success'] is False
    assert body['output_ok'] is False
    assert body['state_cleared'] is True, "应说明软件侧已清零，避免操作员重复点"


def test_api_reset_ok_still_returns_200(client, auth_headers, app):
    app.alarm_manager.alarm_output = object()
    app.alarm_manager.reset_alarm.return_value = True

    resp = client.post('/api/alarm-output/reset', headers=auth_headers)

    assert resp.status_code == 200
    assert resp.get_json()['output_ok'] is True
