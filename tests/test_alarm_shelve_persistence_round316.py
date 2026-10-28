# -*- coding: utf-8 -*-
"""round 316：「重启后状态不一致」这一族的最后一处 —— 报警屏蔽（ISA-18.2 Shelving）。

## 缺陷（探针实测）

```
m.shelve_alarm('r1','d1','press','维护','op1',30)   → 屏蔽生效，触发时不播报
—— 进程重启 ——
重启后 _shelved_alarms: []                          ← 屏蔽全丢
重启后触发 → 声光 1 次、前端推送 1 次                ← 立刻重新刷屏
```

维护窗口内重启（改配置 / 升级 / 崩溃重启）是最常见的运维动作，
而它会让**正在被屏蔽的刷屏报警立刻重新响起来** —— 操作员正蹲在设备旁边检修。

ISA-18.2 里 shelving 是「有原因、有操作人、有超时」的动作，
**只该由超时或人工解除结束**，重启不该撤销它。

## 修法

复用 round 315 加的 `system_state`（KV）表，key = `alarm_shelves`：
· `shelve_alarm` / `unshelve_alarm` 成功后落库；
· `__init__` 里 `_restore_shelves()` 恢复，**过期的不恢复**；
· 手动屏蔽（`shelved_until = None`）**必须恢复** —— 它本来就没有超时。

## ⚠️ 本轮抓到一个真实自锁死

第一版把 `self._persist_shelves()` 写在了 `unshelve_alarm()` 的
`with self._state_lock:` **块内**，而 `_state_lock` 是
`threading.Lock()`（**非可重入**）→ 持锁调用会**自锁死**
（整个报警模块静默停摆，无任何报错）。探针直接 SIGTERM 挂住。

更值得记的是：round 313 加的那个「非可重入锁 + 嵌套调用」闸门
**当时没抓到** —— 它只认 `threading.Lock()`，而这里是
`from threading import Lock` 之后的裸 `Lock()`。闸门对 `AlarmManager`
**恒绿**。已补 `Name` 形式 + 一条反向对照。
"""

import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from 存储层.database import Database
from 报警层.alarm_manager import AlarmManager

RULE = {'id': 'r1', 'level': 'warning', 'name': '压力波动',
        'condition': 'greater_than', 'threshold': 1.0,
        'delay': 0, 'deadband': 0, 'area': 'all'}
KEY = ('r1', 'd1', 'press')


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'shelve.db'))


def _mgr(db):
    m = AlarmManager(db)
    m.rules['r1'] = RULE
    m._rules_index[('d1', 'press')] = [('r1', RULE)]
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


def test_shelve_survives_restart_and_still_suppresses(db):
    """屏蔽跨重启保持，且重启后触发**不播报**。"""
    m1 = _mgr(db)
    try:
        assert m1.shelve_alarm('r1', 'd1', 'press', '维护', 'op1', 30) is True
    finally:
        _stop(m1)

    m2 = _mgr(db)
    try:
        assert KEY in m2._shelved_alarms, (
            '屏蔽重启后丢失 —— 正在检修时那条刷屏报警会立刻重新响')
        assert m2.get_shelved_alarms()[0]['reason'] == '维护'

        m2.check_alarm('d1', 'press', 2.0, datetime(2026, 10, 29, 4, 0, 0))
        assert m2.alarm_output.trigger_alarm.call_count == 0, (
            '重启后屏蔽失效 —— 声光被拉响了')
        assert m2._websocket_emit.call_count == 0, (
            '重启后屏蔽失效 —— 前端弹窗重新刷屏')
    finally:
        _stop(m2)


def test_unshelve_clears_persistence(db):
    """反向对照：解除后再重启，不该又变回屏蔽状态。"""
    m1 = _mgr(db)
    try:
        m1.shelve_alarm('r1', 'd1', 'press', '维护', 'op1', 30)
        assert m1.unshelve_alarm('r1', 'd1', 'press') is True
    finally:
        _stop(m1)

    m2 = _mgr(db)
    try:
        assert m2._shelved_alarms == {}, (
            '解除屏蔽没有清库 —— 每次重启都会"复活"成屏蔽，报警永远不响')
    finally:
        _stop(m2)


def test_manual_shelve_never_expires(db):
    """手动屏蔽（`duration_minutes=None`）本来就没有超时，重启必须保持。"""
    m1 = _mgr(db)
    try:
        m1.shelve_alarm('r1', 'd1', 'press', '长期屏蔽', 'op1', None)
    finally:
        _stop(m1)

    m2 = _mgr(db)
    try:
        assert KEY in m2._shelved_alarms
        assert m2._shelved_alarms[KEY].shelved_until is None
    finally:
        _stop(m2)


def test_expired_shelve_is_not_restored(db, caplog):
    """**关键反向对照**：库里留着一条早该过期的屏蔽 → 不许恢复。

    否则一次"带 30 分钟超时的屏蔽"会变成**永久屏蔽** —— 那条报警永远不响。
    """
    m1 = _mgr(db)
    try:
        m1.shelve_alarm('r1', 'd1', 'press', '维护', 'op1', 30)
    finally:
        _stop(m1)

    # 把库里的到期时间改成"已经过去 1 小时"
    payload = db.get_system_state(AlarmManager.STATE_KEY_SHELVES)
    assert isinstance(payload, list) and payload
    payload[0]['shelved_until'] = (datetime.now() - timedelta(hours=1)).isoformat()
    db.set_system_state(AlarmManager.STATE_KEY_SHELVES, payload)

    # ⚠️ 断言必须看**日志**，不能只看 `_shelved_alarms` 最终状态：
    # round 315 踩过这个坑 —— 变异后最终状态可能"恰好一致"（巧合掩盖）。
    # 这里过期项不会触发任何自动恢复动作，所以用日志区分"没恢复"最稳。
    with caplog.at_level(logging.WARNING):
        m2 = _mgr(db)
    try:
        assert KEY not in m2._shelved_alarms, (
            '已过期的屏蔽被恢复了 —— 这条报警变成永久静音')
        restored = [r.getMessage() for r in caplog.records
                    if '恢复' in r.getMessage() and '屏蔽' in r.getMessage()]
        assert not any('恢复 1 条' in m for m in restored), (
            f'过期屏蔽被当成有效项恢复了: {restored}')
        # 过期项应从库里清掉
        assert not (db.get_system_state(AlarmManager.STATE_KEY_SHELVES) or [])
    finally:
        _stop(m2)


def test_no_shelve_no_restore(db):
    """反向对照：没屏蔽过 → 重启后为空。"""
    m = _mgr(db)
    try:
        assert m._shelved_alarms == {}
    finally:
        _stop(m)


def test_bad_payload_entries_are_dropped(db):
    """库里混入非法项 → 丢弃并继续恢复合法的（不许整批失败）。"""
    db.set_system_state(AlarmManager.STATE_KEY_SHELVES, [
        'not-a-dict',
        {'alarm_id': 'r1'},                       # 缺 device/register
        {'alarm_id': 'r1', 'device_id': 'd1', 'register_name': 'press',
         'reason': '维护', 'shelved_by': 'op1', 'shelved_until': None},
    ])

    m = _mgr(db)
    try:
        assert list(m._shelved_alarms) == [KEY], '合法项被非法项连累丢弃了'
    finally:
        _stop(m)
