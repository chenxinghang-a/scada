# -*- coding: utf-8 -*-
"""core/index_bootstrap.py 的回归测试。

覆盖：
1. 空库（只有表、没有任何索引）上调用 ensure_indexes() → 清单里的索引全部建成。
2. 幂等性 → 连续调用两次，第二次 created 为空、already_existed 等于总数。
3. 收益证明 → 建索引前 EXPLAIN QUERY PLAN 是 SCAN，建索引后走索引。
4. 单条 DDL 失败不中断其它索引，失败项进 failed。
5. 传真实 Database 实例也能正常工作。
"""

import sqlite3
import sys
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import index_bootstrap
from core.index_bootstrap import INDEX_DDL, ensure_indexes, _index_name


# --------------------------------------------------------------------------
# 裸 schema：只有表，没有任何 CREATE INDEX（模拟"缺索引"的历史库）
# --------------------------------------------------------------------------
BARE_SCHEMA = [
    '''CREATE TABLE realtime_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL, register_name TEXT NOT NULL,
        value REAL, unit TEXT, timestamp DATETIME NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(device_id, register_name))''',
    '''CREATE TABLE history_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL, register_name TEXT NOT NULL,
        value REAL, unit TEXT, timestamp DATETIME NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE alarm_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alarm_id TEXT NOT NULL, device_id TEXT NOT NULL, register_name TEXT NOT NULL,
        alarm_level TEXT NOT NULL, alarm_message TEXT, threshold REAL, actual_value REAL,
        timestamp DATETIME NOT NULL, trigger_count INTEGER DEFAULT 1,
        last_trigger_time DATETIME, last_value REAL,
        acknowledged BOOLEAN DEFAULT 0, acknowledged_at DATETIME, acknowledged_by TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE device_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL, status TEXT NOT NULL, message TEXT,
        timestamp DATETIME NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE history_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL, register_name TEXT NOT NULL,
        avg_value REAL, min_value REAL, max_value REAL, sample_count INTEGER,
        archive_date DATE NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''',
]


def _plan(conn, sql, params=()):
    """返回 EXPLAIN QUERY PLAN 的可读文本。"""
    rows = conn.execute('EXPLAIN QUERY PLAN ' + sql, params).fetchall()
    return ' | '.join(str(r[-1]) for r in rows)


@pytest.fixture
def bare_db(tmp_path):
    """建一个"有表没索引"的临时库，并灌入少量数据供 planner 估算。"""
    db_path = tmp_path / 'bare.db'
    conn = sqlite3.connect(str(db_path))
    for ddl in BARE_SCHEMA:
        conn.execute(ddl)
    conn.executemany(
        'INSERT INTO history_data (device_id, register_name, value, unit, timestamp) VALUES (?,?,?,?,?)',
        [(f'dev_{i % 20}', f'reg_{i % 8}', i * 0.1, 'C',
          f'2026-0{1 + i % 9}-{1 + i % 27:02d} {i % 24:02d}:{i % 60:02d}:{i % 60:02d}')
         for i in range(5000)])
    conn.executemany(
        '''INSERT INTO alarm_records
           (alarm_id, device_id, register_name, alarm_level, alarm_message, threshold,
            actual_value, timestamp, acknowledged) VALUES (?,?,?,?,?,?,?,?,?)''',
        [(f'alm_{i % 50}', f'dev_{i % 20}', f'reg_{i % 8}',
          ['info', 'warning', 'critical'][i % 3], 'msg', 80.0, 90.0,
          f'2026-0{1 + i % 9}-{1 + i % 27:02d} {i % 24:02d}:{i % 60:02d}:{i % 60:02d}',
          i % 2) for i in range(3000)])
    conn.executemany(
        'INSERT INTO device_status (device_id, status, message, timestamp) VALUES (?,?,?,?)',
        [(f'dev_{i % 20}', ['running', 'stopped', 'fault'][i % 3], 'm',
          f'2026-0{1 + i % 9}-{1 + i % 27:02d} {i % 24:02d}:{i % 60:02d}:00')
         for i in range(500)])
    conn.commit()
    conn.execute('ANALYZE')
    conn.commit()
    yield conn
    conn.close()


def _all_index_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall()}


# --------------------------------------------------------------------------
# 1. 空库 → 索引全部建成
# --------------------------------------------------------------------------
def test_creates_all_indexes_on_empty_db(bare_db):
    expected = {_index_name(ddl) for ddl in INDEX_DDL}
    assert expected, "索引清单不能为空，否则这个测试毫无意义"

    before = _all_index_names(bare_db)
    assert not (expected & before), f"前置条件不成立，裸库里不应已有这些索引: {expected & before}"

    result = ensure_indexes(bare_db)

    assert set(result['created']) == expected, f"未全部建成: 缺 {expected - set(result['created'])}"
    assert result['already_existed'] == []
    assert result['failed'] == []

    after = _all_index_names(bare_db)
    assert expected <= after, f"sqlite_master 里缺少索引: {expected - after}"


# --------------------------------------------------------------------------
# 2. 幂等性
# --------------------------------------------------------------------------
def test_ensure_indexes_is_idempotent(bare_db):
    first = ensure_indexes(bare_db)
    assert len(first['created']) == len(INDEX_DDL)

    # 第二次：不抛异常、created 为空、already_existed 等于总数
    second = ensure_indexes(bare_db)
    assert second['created'] == []
    assert second['failed'] == []
    assert len(second['already_existed']) == len(INDEX_DDL)

    # 第三次也一样，确认不是"第二次特例"
    third = ensure_indexes(bare_db)
    assert third['created'] == []
    assert len(third['already_existed']) == len(INDEX_DDL)


# --------------------------------------------------------------------------
# 3. 收益证明：建索引前 SCAN，建索引后走索引
# --------------------------------------------------------------------------
BENEFIT_CASES = [
    # (用例名, SQL, 参数, 期望被用上的索引)
    ('history 纯时间范围',
     'SELECT COUNT(*) FROM history_data WHERE timestamp < ?',
     ('2026-06-01 00:00:00',),
     'idx_history_timestamp'),
    ('alarm 纯时间范围',
     'SELECT COUNT(*) FROM alarm_records WHERE timestamp BETWEEN ? AND ?',
     ('2026-01-01 00:00:00', '2026-12-31 00:00:00'),
     'idx_alarm_timestamp'),
    ('alarm 设备+级别过滤',
     'SELECT * FROM alarm_records WHERE device_id = ? AND alarm_level = ? ORDER BY timestamp DESC LIMIT 100',
     ('dev_1', 'warning'),
     'idx_alarm_device_level_time'),
    ('device_status 设备最新状态',
     'SELECT * FROM device_status WHERE device_id = ? ORDER BY timestamp DESC LIMIT 1',
     ('dev_1',),
     'idx_device_status_device_time'),
]


@pytest.mark.parametrize('name,sql,params,expected_index', BENEFIT_CASES)
def test_index_is_actually_used_by_query_planner(bare_db, name, sql, params, expected_index):
    """最关键的一条：证明索引真的被查询计划用上了，而不是白建。"""
    plan_before = _plan(bare_db, sql, params)
    assert plan_before.startswith('SCAN'), (
        f'[{name}] 前置条件不成立：建索引前应当是全表扫描，实际: {plan_before}')

    ensure_indexes(bare_db)

    plan_after = _plan(bare_db, sql, params)
    assert not plan_after.startswith('SCAN'), (
        f'[{name}] 建索引后仍然是全表扫描，索引没被用上: {plan_after}')
    assert 'SEARCH' in plan_after, f'[{name}] 建索引后未走 SEARCH: {plan_after}'
    assert expected_index in plan_after, (
        f'[{name}] 建索引后用的不是预期索引 {expected_index}: {plan_after}')


# --------------------------------------------------------------------------
# 4. 单条失败不中断
# --------------------------------------------------------------------------
def test_single_failed_ddl_does_not_block_others(bare_db, monkeypatch):
    bad_name = 'idx_bogus_missing_table'
    bad_ddl = f'CREATE INDEX IF NOT EXISTS {bad_name} ON no_such_table(whatever)'
    monkeypatch.setattr(index_bootstrap, 'INDEX_DDL', [bad_ddl] + list(INDEX_DDL))

    result = ensure_indexes(bare_db)

    assert [f['index'] for f in result['failed']] == [bad_name], (
        f"失败项未被正确记录: {result['failed']}")
    assert result['failed'][0]['error'], '失败项必须带错误原因'

    # 其它索引仍然全部建成
    expected_good = {_index_name(ddl) for ddl in INDEX_DDL}
    assert set(result['created']) == expected_good
    assert expected_good <= _all_index_names(bare_db)

    # 失败的索引名不会污染结果
    assert bad_name not in result['created']
    assert bad_name not in _all_index_names(bare_db)


def test_failure_does_not_raise(bare_db, monkeypatch):
    monkeypatch.setattr(index_bootstrap, 'INDEX_DDL',
                        ['CREATE INDEX IF NOT EXISTS idx_x ON nope(a)',
                         'CREATE INDEX IF NOT EXISTS idx_y ON nope2(b)'])
    result = ensure_indexes(bare_db)  # 不应抛异常
    assert len(result['failed']) == 2
    assert result['created'] == []


# --------------------------------------------------------------------------
# 5. 传真实 Database 实例（接线契约）
# --------------------------------------------------------------------------
def test_ensure_indexes_accepts_database_instance(tmp_path):
    from 存储层.database import Database

    db = Database(str(tmp_path / 'scada.db'))
    try:
        result = ensure_indexes(db)
        assert result['failed'] == []
        assert len(result['created']) + len(result['already_existed']) == len(INDEX_DDL)

        existing = _all_index_names(db._local.connection)
        for ddl in INDEX_DDL:
            assert _index_name(ddl) in existing

        # 再调一次必须幂等
        again = ensure_indexes(db)
        assert again['created'] == []
        assert len(again['already_existed']) == len(INDEX_DDL)
    finally:
        db.close()
