# -*- coding: utf-8 -*-
"""数据归档：同一天不能被反复归档，也不能把「半天」当成整天（round 229）。

两个缺陷叠在一起，**归档表看起来一直有数据**，只是数据是错的：

1. **归档表没有唯一约束 → `INSERT OR IGNORE` 没有冲突可忽略**
   归档任务每天跑一次（`core/maintenance.py`，24h 间隔），每次归档
   「`archive_days` 天之前」的全部数据。而原始数据要到 `delete_days`（30 天）
   才删，于是第 7 天归档的那一天，第 8 天又被归档一次……直到第 30 天。
   同一个 `(设备, 寄存器, 日期)` 最多堆出 **24 条重复行**：
   归档查询返回重复点、归档表体积涨 24 倍。

2. **截止时间没对齐天边界 → 存下「半天均值」**
   `cutoff = now - 7d`（含时分秒）。第 8 天 10:00 跑归档时，第 1 天只有
   00:00–10:00 的采样落在 cutoff 之前，于是归档表里存的是**半天均值**；
   等第 9 天整天都过了，`INSERT OR IGNORE` 又因为已有记录而**忽略**掉
   完整那一天的聚合 —— 那天的日均值永远只有半天数据，且没有任何提示。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from 存储层.database import ARCHIVE_TABLE, Database


def _db(tmp_path) -> Database:
    return Database(str(tmp_path / 'archive.db'))


def _seed(database: Database, day: datetime, n: int = 4, register: str = 'temp'):
    """在某一天内均匀撒 n 条采样（默认从当天 00:00 起每小时一条）。"""
    rows = [
        {
            'device_id': 'd1', 'register_name': register, 'value': float(i),
            'unit': 'C', 'timestamp': day + timedelta(hours=i),
        }
        for i in range(n)
    ]
    database.insert_data_batch(rows)


def _archive_rows(database: Database) -> list[tuple]:
    with database.get_connection(readonly=True) as conn:
        cur = conn.cursor()
        cur.execute(
            f'SELECT device_id, register_name, archive_date, avg_value, sample_count '
            f'FROM {ARCHIVE_TABLE} ORDER BY archive_date, register_name'
        )
        return [tuple(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 1. 重复归档
# ---------------------------------------------------------------------------

def test_archive_is_idempotent_across_daily_runs(tmp_path):
    """同一份数据归档两次，归档表里不能出现两行。"""
    db = _db(tmp_path)
    day = (datetime.now() - timedelta(days=10)).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        _seed(db, day)

        first = db.archive_old_data(archive_days=7, delete_days=30)
        after_first = _archive_rows(db)
        second = db.archive_old_data(archive_days=7, delete_days=30)
        after_second = _archive_rows(db)

        assert after_first, '第一次归档就没写进去'
        assert len(after_second) == len(after_first), (
            f'第二次归档又插了一遍：{len(after_first)} → {len(after_second)} 行。'
            '归档任务每天跑一次，没有唯一约束的话同一天会堆出 24 条重复行'
        )
        assert second['archived'] == 0, (
            f"第二次 archived={second['archived']}（应为 0）—— INSERT OR IGNORE 没有生效"
        )
    finally:
        db.close()


def test_archive_table_has_unique_index(tmp_path):
    """结构层面钉住：归档表必须有 (设备, 寄存器, 日期) 唯一索引。"""
    db = _db(tmp_path)
    try:
        with db.get_connection(readonly=True) as conn:
            cur = conn.cursor()
            cur.execute(f'PRAGMA index_list({ARCHIVE_TABLE})')
            indexes = cur.fetchall()
            unique_cols = []
            for idx in indexes:
                # PRAGMA index_list: (seq, name, unique, origin, partial)
                if not idx[2]:
                    continue
                cur.execute(f'PRAGMA index_info({idx[1]})')
                unique_cols.append([r[2] for r in cur.fetchall()])
        assert ['device_id', 'register_name', 'archive_date'] in unique_cols, (
            f'没有覆盖 (device_id, register_name, archive_date) 的唯一索引，实际: {unique_cols}'
        )
    finally:
        db.close()


def test_existing_duplicates_are_deduped_on_startup(tmp_path):
    """历史库里已经堆了重复行时，启动必须去重后再建唯一索引（否则建索引会失败）。"""
    path = str(tmp_path / 'legacy.db')
    db = Database(path)
    db.close()

    # 模拟旧版本：去掉唯一索引，塞入同一 (设备,寄存器,日期) 的 3 条重复行
    conn = sqlite3.connect(path)
    conn.execute('DROP INDEX IF EXISTS idx_archive_unique')
    for avg in (1.0, 2.0, 3.0):
        conn.execute(
            f'INSERT INTO {ARCHIVE_TABLE} '
            f'(device_id, register_name, avg_value, min_value, max_value, sample_count, archive_date) '
            f'VALUES (?, ?, ?, ?, ?, ?, ?)',
            ('d1', 'temp', avg, avg, avg, 10, '2026-01-01'),
        )
    conn.commit()
    conn.close()

    # 重新打开 → 启动流程应当去重 + 建唯一索引
    db2 = Database(path)
    try:
        rows = _archive_rows(db2)
        assert len(rows) == 1, f'启动没去重，仍有 {len(rows)} 条重复行'
        assert rows[0][3] == 1.0, '去重应保留最早的那一条（id 最小）'
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# 2. 半天不能被当成整天
# ---------------------------------------------------------------------------

def test_partial_day_is_not_archived(tmp_path):
    """正好落在 cutoff 当天（尚未过完）的数据不能归档 —— 否则存下的是半天均值。

    注意日期关系（第一版这里算错了）：
    * cutoff = `now - archive_days` 那一天的 **00:00**；
    * 所以「还没过完的那一天」= cutoff 当天 10:00（10:00 > 00:00 → 不在归档范围内）；
    * 「已过完的一天」= 再往前一天的 10:00（整天的数据都在 cutoff 之前）。
    """
    db = _db(tmp_path)
    now = datetime.now()
    try:
        cutoff_day_10am = (now - timedelta(days=7)).replace(
            hour=10, minute=0, second=0, microsecond=0)     # 与 cutoff 同一天 → 未过完
        earlier_day_10am = (now - timedelta(days=8)).replace(
            hour=10, minute=0, second=0, microsecond=0)     # 早于 cutoff 整天 → 已过完

        _seed(db, cutoff_day_10am, n=3, register='partial')
        _seed(db, earlier_day_10am, n=3, register='complete')

        db.archive_old_data(archive_days=7, delete_days=30)
        rows = _archive_rows(db)
        registers = {r[1] for r in rows}

        assert 'complete' in registers, '已过完的那一天没被归档'
        assert 'partial' not in registers, (
            '把「还没过完的那一天」也归档了 —— 存进去的是半天均值，'
            '而且之后整天数据齐了也不会再更新（INSERT OR IGNORE）'
        )
    finally:
        db.close()


def test_archived_aggregate_covers_full_day(tmp_path):
    """归档出来的 sample_count 必须等于整天实际条数（不是半天）。"""
    db = _db(tmp_path)
    now = datetime.now()
    try:
        day = (now - timedelta(days=12)).replace(hour=0, minute=0, second=0, microsecond=0)
        _seed(db, day, n=6)          # 00:00 ~ 05:00 共 6 条，都在同一天

        db.archive_old_data(archive_days=7, delete_days=30)
        rows = _archive_rows(db)
        assert len(rows) == 1
        assert rows[0][4] == 6, f'归档 sample_count={rows[0][4]}（应为 6）'
    finally:
        db.close()
