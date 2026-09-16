"""数据归档统计的筛选条件回归测试。

背景（2026-09 审计）：`DataArchive.get_compression_stats()` 构造了
`conditions` / `params`，SQL 里也写了 `?` 占位符，但 `cursor.execute()`
**从未接收 params** —— 一旦传入 device_id / start_time / end_time，
就会抛 `sqlite3.ProgrammingError: Incorrect number of bindings supplied`。
即带筛选的统计调用一直是坏的。

返回结构（dict）：
    {'total_devices': int, 'total_registers': int,
     'total_records': int, 'details': [{device_id, register_name, ...}]}
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from 存储层.data_archive import DataArchive


class _FakeDatabase:
    """最小 Database 替身：提供 get_connection() 上下文管理器"""

    def __init__(self, conn):
        self._conn = conn

    @contextmanager
    def get_connection(self):
        yield self._conn


@pytest.fixture
def archive():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('''
        CREATE TABLE history_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            register_name TEXT,
            timestamp TEXT,
            value REAL
        )
    ''')
    now = datetime.now()
    rows = [
        ('dev1', 'voltage', (now - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'), 220.0),
        ('dev1', 'voltage', (now - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'), 221.0),
        ('dev2', 'current', (now - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'), 5.0),
    ]
    conn.executemany(
        'INSERT INTO history_data (device_id, register_name, timestamp, value) VALUES (?,?,?,?)',
        rows,
    )
    conn.commit()
    return DataArchive(_FakeDatabase(conn))


def test_stats_without_filter(archive):
    """无条件时应汇总全部设备"""
    result = archive.get_compression_stats()
    assert result['total_devices'] == 2, 'dev1 / dev2 两个设备'
    assert result['total_records'] == 3
    assert len(result['details']) == 2


def test_stats_with_device_filter(archive):
    """按 device_id 筛选必须生效（修复前这里会抛 ProgrammingError）"""
    result = archive.get_compression_stats(device_id='dev1')
    assert result['total_devices'] == 1
    assert result['total_records'] == 2
    assert all(d['device_id'] == 'dev1' for d in result['details'])


def test_stats_with_time_range_filter(archive):
    """按时间范围筛选必须生效，且不能因参数缺失而报错"""
    now = datetime.now()
    result = archive.get_compression_stats(
        start_time=now - timedelta(days=1, hours=1),
        end_time=now,
    )
    assert result['total_records'] == 2, (
        f"应只命中最近一天的 2 条，实际 {result['total_records']}"
    )


def test_stats_with_combined_filters(archive):
    """device_id + 时间范围组合筛选"""
    now = datetime.now()
    result = archive.get_compression_stats(
        device_id='dev1',
        start_time=now - timedelta(days=1, hours=1),
        end_time=now,
    )
    assert result['total_devices'] == 1
    assert result['total_records'] == 1
    assert result['details'][0]['device_id'] == 'dev1'
