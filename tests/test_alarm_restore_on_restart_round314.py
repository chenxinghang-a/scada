# -*- coding: utf-8 -*-
"""round 314：进程重启后「条件尚未恢复」的报警会从看板上消失，并重新播报/升级。

## 缺陷（实测，不是推测）

`alarm_states` 是**纯内存**的，而 `alarm_records.cleared = 0` 才是"现场还在发生"。
重启后不恢复的连锁后果：

```
超温 → 操作员确认（acknowledged=1, cleared=0）
  → 进程重启（alarm_states 清空）
  → get_active_alarms() 返回 []          ← 看板显示"无报警"，现场还在超温
  → 下一次采样：当成全新报警
      · 声光/广播/前端弹窗 全部重来一遍
      · acknowledged 变回 False
      · 升级跟踪重新登记 → 300/900/1800s 后再打给三级主管（人早确认过了）
      · 库里同一条持续报警变成第 2 行      ← 报警 KPI 翻倍
```

第二条路径更隐蔽：如果重启后那台设备**通信中断**（不再有采样），
这条报警就**永久消失**，直到条件再次变化才可能被重新发现 ——
而它恰恰是"现场还在超温"这件事唯一的记录。

## 修法

1. `Database.get_uncleared_alarms()`：取 `cleared = 0` 的记录；
2. `AlarmManager._restore_active_alarms()`：`__init__` 末尾恢复 `alarm_states`
   （含 `acknowledged`），**已确认的不进升级跟踪**（人已处理过），
   未确认的把 `first_trigger_time` 传回升级管理器（倒计时接着算，不归零）；
3. `insert_alarm` 去重判据从「未确认 **且** 未恢复」改成「未恢复」——
   按 ISA-18.2，"已确认但条件仍在"还是**同一条报警**，不是新事件。
"""

import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from 存储层.database import Database
from 报警层.alarm_manager import AlarmManager

RULE = {'id': 'r1', 'level': 'critical', 'name': '超温',
        'condition': 'greater_than', 'threshold': 80.0,
        'delay': 0, 'deadband': 0, 'area': 'all'}
KEY = ('r1', 'd1', 'temp')


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'alarm.db'))


def _manager(db):
    """构造一个「重启后」的 AlarmManager（只装本组用例需要的依赖）。"""
    m = AlarmManager(db)
    m.rules['r1'] = RULE
    m._rules_index[('d1', 'temp')] = [('r1', RULE)]
    m.alarm_output = MagicMock()
    m.alarm_output.enabled = True
    m.broadcast_system = MagicMock()
    m.broadcast_system.enabled = True
    m._websocket_emit = MagicMock()
    return m


def _stop(m):
    try:
        m.stop()
    except Exception:       # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 1. DB 侧
# ---------------------------------------------------------------------------

def test_get_uncleared_alarms_filters_by_cleared(db):
    db.insert_alarm('r1', 'd1', 'temp', 'critical', '超温', 80.0, 95.0,
                    datetime(2026, 10, 29, 1, 0, 0))
    db.insert_alarm('r2', 'd2', 'press', 'warning', '超压', 1.0, 1.2,
                    datetime(2026, 10, 29, 1, 1, 0))
    assert db.clear_alarm('r2', 'd2', 'press') is True

    rows = db.get_uncleared_alarms()
    assert [r['alarm_id'] for r in rows] == ['r1'], (
        '已恢复的报警被当成"未恢复"返回了 —— 重启后会把它重新显示成活跃')


# ---------------------------------------------------------------------------
# 2. 重启恢复
# ---------------------------------------------------------------------------

def test_confirmed_alarm_is_restored_but_not_reescalated(db, caplog):
    """已确认 + 未恢复 → 恢复进看板，但**不进升级跟踪**、不重新播报。"""
    m1 = _manager(db)
    try:
        m1.check_alarm('d1', 'temp', 95.0, datetime(2026, 10, 29, 1, 0, 0))
        m1.acknowledge_alarm('r1', 'd1', 'temp', 'op1')
    finally:
        _stop(m1)
    assert len(db.get_alarm_records(limit=10)) == 1

    with caplog.at_level(logging.WARNING):
        m2 = _manager(db)      # 重启（__init__ 里恢复）

    assert m2.alarm_states[KEY]['acknowledged'] is True, '重启后已确认标记丢了'
    active = m2.get_active_alarms()
    assert [a['alarm_id'] for a in active] == ['r1'], (
        '重启后看板看不到这条报警 —— 现场还在超温，界面却显示"无报警"')
    assert getattr(m2._escalation_manager, '_states', {}) == {}, (
        '已确认的报警重启后被重新登记升级 —— 半夜会再打给三级主管')
    assert any('恢复' in r.getMessage() for r in caplog.records)

    # 再采样：不该重新响、不该新增行
    m2.check_alarm('d1', 'temp', 95.0, datetime(2026, 10, 29, 1, 5, 0))
    assert m2.alarm_output.trigger_alarm.call_count == 0, (
        '重启后同一条件再次采样又把声光拉响了 —— 操作员已经确认过')
    assert len(db.get_alarm_records(limit=10)) == 1, (
        '同一条持续报警在历史里多了一行 —— 报警 KPI 会翻倍')
    assert m2.alarm_states[KEY]['acknowledged'] is True
    _stop(m2)


def test_unconfirmed_alarm_resumes_escalation_clock(db):
    """未确认 + 未恢复 → 恢复升级跟踪，且**倒计时接着算**（不归零）。"""
    m1 = _manager(db)
    try:
        m1.check_alarm('d1', 'temp', 95.0, datetime(2026, 10, 29, 1, 0, 0))
    finally:
        _stop(m1)

    m2 = _manager(db)
    states = getattr(m2._escalation_manager, '_states', {})
    assert len(states) == 1, '未确认的报警重启后没有恢复升级跟踪 —— 永远不会升级'

    state = next(iter(states.values()))
    # 起点应接近库里的 timestamp（2026-10-29 01:00），而不是"现在"
    expected = datetime(2026, 10, 29, 1, 0, 0).timestamp()
    assert abs(state.first_trigger_time - expected) < 5, (
        f'升级倒计时被重置成"重启时刻"（{state.first_trigger_time} vs {expected}）'
        '—— 一条已经拖了很久的报警重启后要重新等满超时')
    _stop(m2)


def test_cleared_alarm_is_not_restored(db):
    """反向对照：已恢复的报警重启后**不该**出现在活跃列表里。

    ⚠️ `AlarmManager` **没有** `clear_alarm()` 方法 —— "条件恢复"是
    `check_alarm()` 里 CLEAR 分支的副作用（写 `cleared = 1` 并删掉内存状态）。
    第一版用例调了个不存在的方法 → AttributeError 在 `_stop()` 之前抛出 →
    后台线程没被停 → **pytest 退出时挂住（120s SIGTERM）**。
    这里走真实路径，并用 try/finally 保证线程一定被停。
    """
    m1 = _manager(db)
    try:
        m1.check_alarm('d1', 'temp', 95.0, datetime(2026, 10, 29, 1, 0, 0))
        # 条件回到正常范围 → CLEAR 分支 → cleared = 1
        m1.check_alarm('d1', 'temp', 20.0, datetime(2026, 10, 29, 1, 2, 0))
    finally:
        _stop(m1)

    rows = db.get_alarm_records(limit=10)
    assert rows and rows[0]['cleared'] == 1, '前提不成立：报警没有被标记为已恢复'

    m2 = _manager(db)
    try:
        assert m2.alarm_states == {}, '已恢复的报警被重启恢复了'
        assert m2.get_active_alarms() == []
    finally:
        _stop(m2)


def test_empty_db_restores_nothing(db):
    """反向对照：空库恢复 0 条，不许报错。"""
    m = _manager(db)
    try:
        assert m.alarm_states == {}
    finally:
        _stop(m)


# ---------------------------------------------------------------------------
# 3. insert_alarm 去重：已确认但未恢复 = 同一条报警
# ---------------------------------------------------------------------------

def test_acknowledged_uncleared_alarm_is_not_a_new_record(db):
    """按 ISA-18.2，「已确认但条件仍在」还是**同一条**报警，不该新建行。"""
    db.insert_alarm('r1', 'd1', 'temp', 'critical', '超温', 80.0, 95.0,
                    datetime(2026, 10, 29, 1, 0, 0))
    db.acknowledge_alarm('r1', 'op1', 'd1', 'temp')

    # 条件一直没恢复，又触发一次
    db.insert_alarm('r1', 'd1', 'temp', 'critical', '超温', 80.0, 96.0,
                    datetime(2026, 10, 29, 1, 5, 0))

    rows = db.get_alarm_records(limit=10)
    assert len(rows) == 1, (
        '已确认但未恢复的报警被当成新报警插了一行 —— 同一条持续报警重复计数')
    assert rows[0]['trigger_count'] == 2, '触发次数没有累加'
    assert rows[0]['acknowledged'] == 1, '已确认标记被新插入的行冲掉了'


def test_cleared_alarm_still_creates_new_record(db):
    """反向对照：**已恢复**的报警再次触发，才是新的一次报警。"""
    db.insert_alarm('r1', 'd1', 'temp', 'critical', '超温', 80.0, 95.0,
                    datetime(2026, 10, 29, 1, 0, 0))
    db.clear_alarm('r1', 'd1', 'temp', datetime(2026, 10, 29, 1, 2, 0))
    db.insert_alarm('r1', 'd1', 'temp', 'critical', '超温', 80.0, 97.0,
                    datetime(2026, 10, 29, 2, 0, 0))

    rows = db.get_alarm_records(limit=10)
    assert len(rows) == 2, '条件恢复后的再次触发被合并进旧记录了 —— 丢失第二次报警的开始时间'
