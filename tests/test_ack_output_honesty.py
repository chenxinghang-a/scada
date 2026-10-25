# -*- coding: utf-8 -*-
"""确认报警时"蜂鸣器真的停了吗"必须如实上报（round 281）。

缺陷形态
--------
`acknowledge_alarm()` 里那行消音调用**丢弃了返回值**：

    if self.alarm_output and self.alarm_output.enabled:
        self._call_output_method('alarm_output', self.alarm_output, 'acknowledge')

DO 写入失败时蜂鸣器还在响，而接口照样回 `success: true`「报警已确认」。
操作员站在盘前：界面说确认了、现场还在叫 —— 只会以为界面卡了，
或者干脆去拔电源（那会让**所有**报警输出失效）。

`reset_alarm()` 早就修过同类问题（软件侧清零 + 硬件侧如实上报），
但**确认**这条路漏了。

判据：**"报警被确认"（数据库层面）与"蜂鸣器被关掉"（硬件层面）是两件事**，
必须分开报，不能让前者掩盖后者。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

KEY = ('r1', 'dev1', 'temp')


def _manager(output=None):
    from 报警层.alarm_manager import AlarmManager

    db = MagicMock()
    db.get_alarm_records.return_value = []
    db.acknowledge_alarm.return_value = True
    mgr = AlarmManager(db)
    mgr.alarm_output = output
    mgr.alarm_states[KEY] = {
        'alarm_id': KEY[0], 'device_id': KEY[1], 'register_name': KEY[2],
        'first_trigger_time': datetime.now(), 'last_trigger_time': datetime.now(),
        'trigger_count': 1, 'confirmed': False, 'acknowledged': False,
    }
    return mgr


# ---------------------------------------------------------------------------
# 1. 管理器层：消音结果要能被读到
# ---------------------------------------------------------------------------

def test_ack_records_output_success():
    output = MagicMock()
    output.enabled = True
    output.acknowledge.return_value = True

    mgr = _manager(output)
    assert mgr.acknowledge_alarm(*KEY, acknowledged_by='op') is True
    assert mgr.get_last_ack_output_ok() is True


def test_ack_records_output_failure():
    output = MagicMock()
    output.enabled = True
    output.acknowledge.return_value = False        # DO 写失败

    mgr = _manager(output)
    assert mgr.acknowledge_alarm(*KEY, acknowledged_by='op') is True, (
        '确认本身成功了（数据库已更新），不该因为硬件失败而返回 False')
    assert mgr.get_last_ack_output_ok() is False, (
        '消音失败没有被记录下来 —— 接口会照样回"报警已确认"')


def test_ack_records_exception_as_failure():
    output = MagicMock()
    output.enabled = True
    output.acknowledge.side_effect = RuntimeError('DO 超时')

    mgr = _manager(output)
    mgr.acknowledge_alarm(*KEY, acknowledged_by='op')
    assert mgr.get_last_ack_output_ok() is False


def test_ack_without_output_counts_as_ok():
    """没配声光输出 → 不存在"消音失败"，按成功算。"""
    mgr = _manager(output=None)
    mgr.acknowledge_alarm(*KEY, acknowledged_by='op')
    assert mgr.get_last_ack_output_ok() is True


def test_ack_failure_does_not_set_output_flag():
    """确认本身失败（数据库没更新）时，不该把上一次的消音结果留在这里误导调用方。"""
    from 报警层.alarm_manager import AlarmManager

    db = MagicMock()
    db.acknowledge_alarm.return_value = False
    mgr = AlarmManager(db)
    mgr._last_ack_output_ok = False        # 模拟上一次失败

    output = MagicMock()
    output.enabled = True
    output.acknowledge.return_value = True
    mgr.alarm_output = output

    assert mgr.acknowledge_alarm(*KEY, acknowledged_by='op') is False
    # 没确认成功 → 不该执行消音，标志位保持原样（调用方看 success=False 即可）
    output.acknowledge.assert_not_called()


# ---------------------------------------------------------------------------
# 2. 与复位路径的语义一致
# ---------------------------------------------------------------------------

def test_reset_and_ack_share_semantics():
    """两条路径都必须是"软件侧成功 + 硬件侧如实上报"。"""
    output = MagicMock()
    output.enabled = True
    output.acknowledge.return_value = False
    output.reset.return_value = False

    mgr = _manager(output)
    # 确认：软件成功
    assert mgr.acknowledge_alarm(*KEY, acknowledged_by='op') is True
    assert mgr.get_last_ack_output_ok() is False
    # 复位：硬件失败 → 返回 False
    assert mgr.reset_alarm() is False
