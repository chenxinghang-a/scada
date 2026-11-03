# -*- coding: utf-8 -*-
"""round 356：报警层的 forget_device。

`AlarmManager` 一直没有 `forget_device`，也不在 `_FORGET_DEVICE_HOLDERS` 里。
一台设备被删除后，它在报警层的状态原样留着：

· `alarm_states` 里「已触发」标记还在 → 同 id 设备重加时**直接继承「已经报过」**，
  第一次真的超温不报警（边沿判断认为状态没变化）；
· `_shelved_alarms` 里未到期的旁路继续生效 → 新设备**默认被旁路**；
· `_emit_history` / `_acknowledge_history` 的时间戳残留 → 前几条报警被当重复压掉；
· 升级计时器继续跑，给一台不存在的设备发升级通知。
"""

from __future__ import annotations

import pytest


def _am():
    from 报警层.alarm_manager import AlarmManager
    return AlarmManager({})


def test_alarm_manager_forget_device_clears_all_state():
    am = _am()
    k_d1 = ('r1', 'd1', 'temp')
    k_d10 = ('r1', 'd10', 'temp')       # 前缀/子串不得误伤

    am.alarm_states[k_d1] = {'triggered': True}
    am.alarm_states[k_d10] = {'triggered': True}
    am._shelved_alarms[k_d1] = object()
    am._emit_history[k_d1] = 0.0
    am._acknowledge_history[k_d1] = 0.0

    assert am.forget_device('d1') is True

    assert k_d1 not in am.alarm_states, '已触发标记没清 —— 重加设备不会再报警'
    assert k_d1 not in am._shelved_alarms, '旁路状态没清 —— 重加设备默认被旁路'
    assert k_d1 not in am._emit_history, '推送冷却没清'
    assert k_d1 not in am._acknowledge_history, '确认记录没清'
    assert k_d10 in am.alarm_states, '误伤了 d10'


def test_alarm_manager_forget_device_returns_false_when_nothing():
    """反向对照：没有该设备的任何状态时返回 False。"""
    am = _am()
    assert am.forget_device('nobody') is False


def test_escalation_forget_device_clears_tracking():
    """升级跟踪用自己的字符串 key，必须靠 `device_id` 字段匹配。

    不能用 `key.split(':')` 拆 —— 自定义规则 id 里含冒号时会错位。
    """
    from 报警层.alarm_escalation import AlarmEscalationManager

    mgr = AlarmEscalationManager({})
    mgr.record_alarm('rule:with:colons', 'd1', 'temp')   # id 本身含冒号
    mgr.record_alarm('r2', 'd10', 'temp')

    assert mgr.forget_device('d1') == 1, '含冒号的 alarm_id 没匹配上'
    assert len(mgr._states) == 1
