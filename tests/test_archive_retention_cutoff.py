"""归档/删除两条时间线的 cutoff 回归测试。

背景（2026-09 缺陷审计）
------------------------
`Database.archive_old_data(archive_days=7, delete_days=30)` 的契约是：

    把 archive_days（7 天）之前的数据按天聚合进 history_archive，
    把 delete_days（30 天）之前的原始数据从 history_data 删除。

但实现里 `delete_cutoff` **算出来从未使用**，DELETE 用的是 `archive_cutoff`：

    archive_cutoff = now - 7天
    delete_cutoff  = now - 30天        # ← 死变量
    INSERT ... WHERE timestamp < archive_cutoff   # 归档 7 天前  ✓
    DELETE ... WHERE timestamp < archive_cutoff   # 删 7 天前    ✗ 应为 30 天前

后果：**原始采样只保留 7 天**，每天静默丢掉 23 天的明细 ——
只剩 history_archive 里的日均值，无法再回溯单条采样、无法做审计。

本测试锁死「7~30 天之间的原始数据必须存活」。
"""
from datetime import datetime, timedelta

import pytest

from 存储层.database import Database


@pytest.fixture
def db(tmp_path):
    """真实 SQLite 库（不用 mock）—— 这个 bug 只有真跑 SQL 才暴露得出来"""
    return Database(str(tmp_path / 'archive_cutoff.db'))


def _insert(db, rows):
    """rows: [(device_id, register_name, value, days_ago)]"""
    now = datetime.now()
    with db.get_connection() as conn:
        conn.executemany(
            'INSERT INTO history_data (device_id, register_name, value, unit, timestamp) '
            'VALUES (?, ?, ?, ?, ?)',
            [(d, r, v, '', (now - timedelta(days=age)).isoformat(sep=' '))
             for d, r, v, age in rows],
        )


def _count(db, where=''):
    with db.get_connection() as conn:
        return conn.execute(f'SELECT COUNT(*) FROM history_data {where}').fetchone()[0]


def _archived(db):
    with db.get_connection() as conn:
        return conn.execute('SELECT COUNT(*) FROM history_archive').fetchone()[0]


def test_raw_data_within_delete_window_survives(db):
    """核心回归：7~30 天之间的原始数据**必须保留**。

    修复前 DELETE 用的是 archive_cutoff（7 天），这批数据会被一起删掉，
    该断言失败。
    """
    _insert(db, [
        ('dev1', 'temp', 20.0, 3),      # 3 天前 —— 两条线都该留
        ('dev1', 'temp', 21.0, 10),     # 10 天前 —— 应保留（在 30 天窗口内）
        ('dev1', 'temp', 22.0, 20),     # 20 天前 —— 应保留
        ('dev1', 'temp', 23.0, 45),     # 45 天前 —— 应删除
    ])

    db.archive_old_data(archive_days=7, delete_days=30)

    assert _count(db) == 3, (
        f'7~30 天之间的原始数据被误删了：剩余 {_count(db)} 条，期望 3 条'
    )
    assert _count(db, "WHERE timestamp < datetime('now', '-30 days')") == 0, \
        '超过 30 天的数据应当被删除'


def test_recent_data_untouched(db):
    """30 天内的数据一条都不能少"""
    _insert(db, [
        ('dev1', 'temp', float(i), i % 30) for i in range(20)
    ])
    before = _count(db)

    db.archive_old_data(archive_days=7, delete_days=30)

    assert _count(db) == before, '30 天内的数据被误删'


def test_data_older_than_delete_days_is_removed(db):
    """超过 delete_days 的原始数据必须删掉（否则表无界增长）"""
    _insert(db, [
        ('dev1', 'temp', 1.0, 5),
        ('dev1', 'temp', 2.0, 40),
        ('dev1', 'temp', 3.0, 60),
    ])

    db.archive_old_data(archive_days=7, delete_days=30)

    assert _count(db) == 1, '超过 30 天的原始数据未被清理'


def test_old_data_is_aggregated_into_archive(db):
    """删除前必须先归档（聚合进 history_archive），不能只删不归"""
    _insert(db, [
        ('dev1', 'temp', 10.0, 40),
        ('dev1', 'temp', 20.0, 40),     # 同一天 → 聚合为一条
        ('dev1', 'temp', 30.0, 41),
    ])

    db.archive_old_data(archive_days=7, delete_days=30)

    assert _archived(db) >= 1, '历史数据被删了却没有进归档表 —— 数据真的丢了'

    # 取**最近**那个归档日（40 天前那两条所在的日期）。
    # 注意不能写 `ORDER BY archive_date LIMIT 1` —— 那取到的是最老的
    # 41 天前那天（只有 1 条），断言必然失败。
    with db.get_connection() as conn:
        row = conn.execute(
            'SELECT sample_count, min_value, max_value FROM history_archive '
            'ORDER BY archive_date DESC LIMIT 1'
        ).fetchone()
    assert row['sample_count'] == 2, '同日两条应聚合为 sample_count=2'
    assert row['min_value'] == 10.0 and row['max_value'] == 20.0


def test_archive_and_delete_windows_are_independent(db):
    """两个窗口必须各自生效：改 delete_days 不应影响归档范围，反之亦然"""
    _insert(db, [
        ('dev1', 'temp', 1.0, 10),   # 在归档窗口外、删除窗口内 → 保留原始
        ('dev1', 'temp', 2.0, 40),   # 两个窗口都外 → 删
    ])

    db.archive_old_data(archive_days=7, delete_days=90)   # 放宽删除窗口

    assert _count(db) == 2, 'delete_days=90 时 40 天前的数据不该被删'
    assert _archived(db) >= 1, '归档窗口仍应生效（7 天前就归档）'


def test_empty_table_is_safe(db):
    """空表不应报错"""
    result = db.archive_old_data()
    assert result['archived'] == 0
    assert result['deleted_history'] == 0
