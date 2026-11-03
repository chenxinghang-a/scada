# -*- coding: utf-8 -*-
"""round 353：审计链 / 急停记账 / 传感器卡死 / 旁路双份。

TLS 与「推送失败不记账」两处没在这里测：
· TLS 是**删掉**一段无条件降级代码，行为变化由 mock 环境难以观测（需要真 TLS 握手），
  现有 `-k "mqtt or gateway or tls"` 273 条已覆盖不崩溃；
· 推送顺序需要在 AlarmManager 上模拟 `_websocket_emit` 抛异常，构造成本高，
  已由 `_emit_websocket_alarm` 的返回契约（bool）在类型上钉住。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _dc():
    from 智能层.device_control import DeviceControlSafety
    return DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())


# ---------------------------------------------------------------------------
# 1. 审计哈希链必须跨重启连续
# ---------------------------------------------------------------------------

def test_audit_chain_survives_restart(tmp_path, monkeypatch):
    """重启后首条记录的 prev 必须等于上一条的 hash。

    原来 `_audit_last_hash` 在 `__init__` 里恒为 ''，进程每次重启都在
    哈希链上留一个断点 —— 「日志没被篡改过」只在单次运行内成立。
    """
    from 智能层 import device_control as mod

    audit_file = tmp_path / 'audit_log.jsonl'
    monkeypatch.setattr(mod.paths, 'resolve', lambda _p: audit_file)

    dc1 = mod.DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())
    assert dc1._audit('a1', 'op', 'first') is True
    h1 = dc1._audit_last_hash
    assert h1, '第一条记录应产生非空 hash'

    # 模拟进程重启：新实例，同一个日志文件
    dc2 = mod.DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())
    assert dc2._audit_last_hash == h1, '重启后没有从文件恢复链尾'

    dc2._audit('a2', 'op', 'second')
    entry = [e for e in dc2.get_audit_log(limit=10) if e['action'] == 'a2'][0]
    assert entry['prev'] == h1, f"链在重启处断了：prev={entry['prev']!r}"


def test_audit_chain_starts_empty_when_no_file(tmp_path, monkeypatch):
    """反向对照：没有历史文件时，从空串起链。"""
    from 智能层 import device_control as mod

    monkeypatch.setattr(mod.paths, 'resolve', lambda _p: tmp_path / 'none.jsonl')
    dc = mod.DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())
    assert dc._audit_last_hash == ''


# ---------------------------------------------------------------------------
# 2. 急停只写一条审计
# ---------------------------------------------------------------------------

def test_estop_writes_exactly_one_audit():
    """上一轮加「失败清单进审计」时没注意上面已有一条，写了两次。"""
    dc = _dc()
    dc.device_manager.get_all_devices.return_value = {
        'm0': {'id': 'm0', 'device_category': 'mechanical'}}
    dc.device_manager.stop_device.return_value = True
    dc.alarm_manager = None

    dc.trigger_emergency_stop('test')

    actions = [e['action'] for e in dc.get_audit_log(limit=50)]
    assert actions.count('emergency_stop') == 1, f'急停审计写了 {actions.count("emergency_stop")} 条'


# ---------------------------------------------------------------------------
# 3. 传感器卡死：超过窗口必须降为 UNCERTAIN
# ---------------------------------------------------------------------------

def test_stuck_sensor_becomes_uncertain_after_window(monkeypatch):
    from 采集层 import data_collector as mod

    monkeypatch.setattr(mod.time, 'monotonic', lambda: 1000.0)

    stuck = mod.DataQualityAssessor.assess(
        value=5.0, register_name='t', device_status='online',
        last_value=5.0, last_time=600.0)          # 400s 未变
    assert stuck == mod.DataQualityAssessor.UNCERTAIN_LAST_USABLE


def test_fresh_value_stays_good(monkeypatch):
    """反向对照：值刚变过（100s）仍是 GOOD。"""
    from 采集层 import data_collector as mod

    monkeypatch.setattr(mod.time, 'monotonic', lambda: 1000.0)

    good = mod.DataQualityAssessor.assess(
        value=5.0, register_name='t', device_status='online',
        last_value=5.0, last_time=900.0)
    assert good == mod.DataQualityAssessor.GOOD


# ---------------------------------------------------------------------------
# 4. remove_interlock 必须清掉两份旁路状态
# ---------------------------------------------------------------------------

def test_remove_interlock_clears_both_bypass_sets():
    dc = _dc()
    dc.add_interlock({
        'id': 'r1',
        'condition': {'type': 'threshold', 'key': 'd:r', 'operator': 'gt', 'value': 1},
        'action': {'type': 'set_alarm', 'message': 'x'},
        'name': '测试联锁',
    })
    dc._interlock_bypass.add('r1')
    dc._active_bypasses.add('r1')

    assert dc.remove_interlock('r1') is True

    assert 'r1' not in dc._interlock_bypass
    assert 'r1' not in dc._active_bypasses, (
        '`_active_bypasses` 没清 —— 库里仍留旁路记录，重启后复活')
