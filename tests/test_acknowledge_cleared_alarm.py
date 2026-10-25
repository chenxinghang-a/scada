# -*- coding: utf-8 -*-
"""确认一条**已恢复**的报警，不许把下一次新报警压掉（round 279）。

缺陷形态
--------
`acknowledge_alarm()` 里，确认成功后无条件执行：

    self._record_acknowledge(alarm_id, device_id, register_name)

这会在 `_acknowledge_history` 里记一个时间戳，`_should_emit()` 随后据此在
**600 秒**内抑制同一 key 的所有推送（"你刚确认过，别刷屏"）。

问题在于：操作员可能在**历史列表**里确认一条**早就恢复**的报警（`alarm_states`
里已经没有它了，`state is None`）。这时：

* 设备 5 分钟后**真的又越限** → 新报警在抑制窗口内被静默压掉：
  界面不弹、声光不响、广播不播；
* 日志里只有一行 debug（`报警WebSocket推送被去重抑制`），没人会看；
* 操作员以为自己刚确认过、系统正常，而现场已经又出问题了。

判据：**"确认一条历史记录"与"确认当前这条报警"是两件事**。
前者不该产生"未来 10 分钟不许再报"的效果。

已恢复报警的确认**仍然要落库**（`acknowledged=1`）—— ISA-18.2 要求
"已恢复但未确认"能被单独统计，人工补确认是合法的操作，只是不该有副作用。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

KEY = ('r1', 'dev1', 'temp')


@pytest.fixture
def am():
    from 报警层.alarm_manager import AlarmManager

    db = MagicMock()
    db.get_alarm_records.return_value = []
    db.get_active_alarms.return_value = []
    db.acknowledge_alarm.return_value = True
    mgr = AlarmManager(db)
    mgr.dedup_config.enabled = True
    mgr.dedup_config.acknowledge_suppress_seconds = 600
    yield mgr
    try:
        mgr.stop()
    except Exception:       # noqa: BLE001
        pass


def _arm_active(am, ts=None):
    ts = ts or datetime(2026, 10, 26, 4, 0, 0)
    am.alarm_states[KEY] = {
        'alarm_id': KEY[0], 'device_id': KEY[1], 'register_name': KEY[2],
        'first_trigger_time': ts, 'last_trigger_time': ts,
        'trigger_count': 1, 'confirmed': False, 'acknowledged': False,
    }


# ---------------------------------------------------------------------------
# 1. 已恢复的报警：确认可以，但不许设抑制
# ---------------------------------------------------------------------------

def test_acknowledging_cleared_alarm_does_not_suppress_next(am):
    """确认一条不在活跃列表里的报警 → 下一次新报警必须照常推送。"""
    # 没有 _arm_active：模拟"这条报警已经恢复了"
    assert am.acknowledge_alarm(*KEY, acknowledged_by='op1') is True

    assert am._should_emit(*KEY) is True, (
        '确认了一条已恢复的报警，却把下一次真正的新报警压掉了 —— '
        '设备再越限时界面不弹、声光不响')


def test_acknowledging_cleared_alarm_still_persists(am):
    """已恢复报警的确认仍要落库（历史记录里 acknowledged=1）。"""
    am.acknowledge_alarm(*KEY, acknowledged_by='op1')
    am.database.acknowledge_alarm.assert_called_once_with(
        KEY[0], 'op1', KEY[1], KEY[2])


def test_acknowledging_cleared_alarm_does_not_touch_state(am):
    """没有活跃状态时不该凭空造一个。"""
    am.acknowledge_alarm(*KEY, acknowledged_by='op1')
    assert KEY not in am.alarm_states


# ---------------------------------------------------------------------------
# 2. 活跃报警：行为不变（仍要抑制，防刷屏）
# ---------------------------------------------------------------------------

def test_acknowledging_active_alarm_suppresses(am):
    _arm_active(am)
    assert am.acknowledge_alarm(*KEY, acknowledged_by='op1') is True

    assert am.alarm_states[KEY]['acknowledged'] is True
    assert am._should_emit(*KEY) is False, (
        '确认当前活跃报警后，冷却期内仍应抑制（这是防刷屏的既有行为）')


def test_active_ack_suppression_expires(am):
    _arm_active(am)
    am.acknowledge_alarm(*KEY, acknowledged_by='op1')

    # 把抑制时间推到窗口之外
    am._acknowledge_history[KEY] -= 601
    assert am._should_emit(*KEY) is True


# ---------------------------------------------------------------------------
# 3. 两种确认的差别只在这一个副作用上
# ---------------------------------------------------------------------------

def test_only_suppression_differs(am):
    """活跃确认 vs 已恢复确认：落库都做，只有抑制窗口不同。"""
    # 已恢复
    am.acknowledge_alarm(*KEY, acknowledged_by='op1')
    assert am._should_emit(*KEY) is True

    # 活跃
    _arm_active(am)
    am.acknowledge_alarm(*KEY, acknowledged_by='op2')
    assert am._should_emit(*KEY) is False
