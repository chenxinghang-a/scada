# -*- coding: utf-8 -*-
"""报警「恢复正常」必须落到数据库（round 260）。

缺陷形态
--------
`alarm_records` 表里**没有"已恢复"这个概念**（没有 `cleared` / `cleared_at` 列），
而条件恢复正常时 `alarm_manager` 只删掉内存里的 `alarm_states`，数据库那行
永远停在 `acknowledged = 0`。后果：

1. 报警历史里**每条记录都像"还没结束"**，报表分不清"已恢复"和"仍在报警"；
2. `get_db_stats()` 的「未确认报警」是个**只增不减**的数（运维看板上的
   未确认数只涨不落，越跑越难看）；
3. 同一条报警几小时后再触发时，`insert_alarm` 的"同一条未确认记录"查询
   把它捞回来只把 `trigger_count + 1` —— **第二次报警的开始时间、
   持续时间全部丢失**，历史里看着像一条持续了几天的报警。

修法：`cleared` / `cleared_at` 两列（含老库迁移）+ `Database.clear_alarm()`
+ 报警清除 / 人工复位两条路径都调用它。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

ALARM_KEY = ('r1', 'dev1', 'temp')


@pytest.fixture
def db(tmp_path):
    from 存储层.database import Database
    database = Database(str(tmp_path / 'alarm.db'))
    yield database
    database.close()


def _trigger(db, when: datetime, key=ALARM_KEY):
    db.insert_alarm(key[0], key[1], key[2], 'warning', '温度过高',
                    80.0, 95.0, when)


def _make_manager(db):
    from 报警层.alarm_manager import AlarmManager
    return AlarmManager(db)


def _arm_active_state(am, ts: datetime, key=ALARM_KEY):
    """手工造一个"正在报警"的内存状态（不走 check_alarm 的触发路径）。"""
    am.alarm_states[key] = {
        'alarm_id': key[0], 'device_id': key[1], 'register_name': key[2],
        'first_trigger_time': ts, 'last_trigger_time': ts,
        'trigger_count': 1, 'confirmed': False, 'acknowledged': False,
    }


def _arm_rule(am, key=ALARM_KEY, threshold=80.0):
    am.rules[key[0]] = {
        'id': key[0], 'name': 'n', 'device_id': key[1], 'register_name': key[2],
        'condition': 'greater_than', 'threshold': threshold,
        'level': 'warning', 'enabled': True,
    }
    am._rebuild_rules_index()


# ---------------------------------------------------------------------------
# 1. 清除必须落库
# ---------------------------------------------------------------------------

def test_clear_writes_database(db):
    _trigger(db, datetime(2026, 10, 24, 8, 0, 0))
    before = db.get_alarm_records(limit=10)
    assert len(before) == 1
    assert before[0]['cleared'] in (0, None)

    assert db.clear_alarm(*ALARM_KEY,
                          cleared_at=datetime(2026, 10, 24, 8, 30, 0)) is True

    after = db.get_alarm_records(limit=10)[0]
    assert after['cleared'] == 1
    assert after['cleared_at'] is not None
    # 清除不等于确认：没被确认过的报警仍然 acknowledged = 0
    assert after['acknowledged'] == 0


def test_clear_is_idempotent(db):
    _trigger(db, datetime.now())
    assert db.clear_alarm(*ALARM_KEY) is True
    assert db.clear_alarm(*ALARM_KEY) is False, '第二次清除不该再命中记录'


def test_clear_without_open_record_returns_false(db):
    """没有活跃记录时返回 False（进程重启后内存重建 / 重复清除都属正常）。"""
    assert db.clear_alarm(*ALARM_KEY) is False


def test_acknowledge_is_not_clear(db):
    """确认与恢复是两件事：确认之后 cleared 仍为 0。"""
    _trigger(db, datetime.now())
    assert db.acknowledge_alarm('r1', 'operator', 'dev1', 'temp') is True
    record = db.get_alarm_records(limit=10)[0]
    assert record['acknowledged'] == 1
    assert record['cleared'] in (0, None)


# ---------------------------------------------------------------------------
# 2. 恢复后再次触发 = 新记录（而不是把老记录 +1）
# ---------------------------------------------------------------------------

def test_retrigger_after_clear_creates_new_record(db):
    _trigger(db, datetime(2026, 10, 24, 8, 0, 0))
    db.clear_alarm(*ALARM_KEY, cleared_at=datetime(2026, 10, 24, 8, 30, 0))

    # 同一条报警 3 小时后再触发
    _trigger(db, datetime(2026, 10, 24, 11, 0, 0))

    records = db.get_alarm_records(limit=10)
    assert len(records) == 2, (
        f'恢复后再次触发没有新建记录（只有 {len(records)} 条）—— '
        '第二次报警的开始时间被并进了老记录')
    newest = max(records, key=lambda r: r['timestamp'])
    assert newest['trigger_count'] == 1, '新记录的 trigger_count 被老记录污染了'


def test_retrigger_while_still_active_increments(db):
    """反向对照：仍活跃时只更新计数（既有正确行为，不许被改坏）。"""
    first = datetime(2026, 10, 24, 8, 0, 0)
    _trigger(db, first)
    _trigger(db, first + timedelta(minutes=5))

    records = db.get_alarm_records(limit=10)
    assert len(records) == 1
    assert records[0]['trigger_count'] == 2


# ---------------------------------------------------------------------------
# 3. 统计口径
# ---------------------------------------------------------------------------

def test_unacknowledged_count_excludes_cleared(db):
    """未确认数只算仍活跃的 —— 否则只增不减。"""
    _trigger(db, datetime(2026, 10, 24, 8, 0, 0))
    assert db.get_database_stats()['unacknowledged_alarms'] == 1

    db.clear_alarm(*ALARM_KEY, cleared_at=datetime(2026, 10, 24, 8, 30, 0))
    stats = db.get_database_stats()
    assert stats['unacknowledged_alarms'] == 0, (
        '已恢复的报警仍被算作"未确认" —— 这个数会只增不减')
    assert stats['active_alarms'] == 0


def test_active_count_ignores_cleared_but_keeps_unacked(db):
    _trigger(db, datetime(2026, 10, 24, 8, 0, 0))
    assert db.get_database_stats()['active_alarms'] == 1

    db.acknowledge_alarm('r1', 'operator', 'dev1', 'temp')
    stats = db.get_database_stats()
    assert stats['active_alarms'] == 1, '确认过的报警仍在持续，算活跃'
    assert stats['unacknowledged_alarms'] == 0


# ---------------------------------------------------------------------------
# 4. 报警管理器侧的接线
# ---------------------------------------------------------------------------

def test_manager_clear_marks_record(db):
    """条件恢复正常（check_alarm 走到清除分支）必须落库。"""
    am = _make_manager(db)
    ts = datetime(2026, 10, 24, 8, 0, 0)

    _trigger(db, ts)
    _arm_active_state(am, ts)
    _arm_rule(am)

    am.check_alarm('dev1', 'temp', 10.0, ts + timedelta(minutes=5))

    assert ALARM_KEY not in am.alarm_states, '内存状态没被清掉'
    records = db.get_alarm_records(limit=10)
    assert records and records[0]['cleared'] == 1, (
        '条件恢复正常后数据库没有标记 cleared —— 历史里这条报警永远"未结束"')


def test_reset_alarm_marks_records(db):
    """人工复位同样要落库。"""
    am = _make_manager(db)
    ts = datetime.now()
    _trigger(db, ts)
    _arm_active_state(am, ts)

    am.reset_alarm(device_id='dev1')

    records = db.get_alarm_records(limit=10)
    assert records and records[0]['cleared'] == 1, '人工复位没有把"已恢复"写进库'


def test_clear_record_failure_does_not_break_alarm_chain(db):
    """落库失败只能记日志，绝不能让报警清除本身失败（静默吞掉也不行）。"""
    am = _make_manager(db)
    ts = datetime(2026, 10, 24, 8, 0, 0)
    _trigger(db, ts)
    _arm_active_state(am, ts)
    _arm_rule(am)

    class Boom:
        def clear_alarm(self, *a, **kw):
            raise RuntimeError('disk full')

    am.database = Boom()
    am.check_alarm('dev1', 'temp', 10.0, ts + timedelta(minutes=5))
    assert ALARM_KEY not in am.alarm_states, '落库失败把报警清除也带崩了'


# ---------------------------------------------------------------------------
# 5. 老库迁移
# ---------------------------------------------------------------------------

def test_legacy_db_gets_new_columns(tmp_path):
    """老库（没有 cleared 列）打开时必须补上，且不报错。"""
    import sqlite3
    from 存储层.database import Database

    legacy = tmp_path / 'legacy.db'
    conn = sqlite3.connect(str(legacy))
    conn.execute('''
        CREATE TABLE alarm_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alarm_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            register_name TEXT NOT NULL,
            alarm_level TEXT NOT NULL,
            timestamp DATETIME NOT NULL
        )
    ''')
    conn.execute("INSERT INTO alarm_records (alarm_id, device_id, register_name,"
                 " alarm_level, timestamp)"
                 " VALUES ('old1', 'd', 't', 'warning', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    db = Database(str(legacy))
    try:
        rows = db.get_alarm_records(limit=5)
        assert len(rows) == 1
        assert rows[0].get('cleared') in (0, None)
        assert db.clear_alarm('old1', 'd', 't') is True
        assert db.get_alarm_records(limit=5)[0]['cleared'] == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 6. 老代码不许回来
# ---------------------------------------------------------------------------

def test_insert_alarm_open_query_checks_cleared():
    """`insert_alarm` 的"仍活跃"判据必须包含 `cleared = 0`。"""
    from pathlib import Path
    import re

    src = Path(__file__).resolve().parents[1] / '存储层' / 'database.py'
    code = re.sub(r"#.*$", "", src.read_text(encoding='utf-8'), flags=re.M)
    open_query = re.search(
        r"SELECT id, trigger_count FROM alarm_records(.{0,400}?)ORDER BY timestamp DESC",
        code, flags=re.S)
    assert open_query, '没找到 insert_alarm 的活跃记录查询'
    assert 'cleared = 0' in open_query.group(1), (
        '"仍活跃"的判据又变回只看 acknowledged —— 恢复后再次触发会被并进老记录')
