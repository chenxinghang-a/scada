# -*- coding: utf-8 -*-
"""归档后删除：只删「已经进过归档表」的行。

背景：归档与删除必须分两个事务（round 352，合并会让全表 DELETE 长时间持写锁
把采集线程饿死）。两个事务之间的窗口里如果有「迟到回填」的旧数据写进来
（时间戳早于 delete_cutoff），原来的删除语句会直接把它抹掉，而它从没进过归档。

这里锁两件事：
1. `_delete_older_than_chunked` 的 `extra_where` 真的生效；
2. `archive_old_data` 传下去的条件确实是「归档表里存在对应日聚合」。
"""

import sqlite3

import pytest

from 存储层.database import _delete_older_than_chunked


class TestChunkedDeleteExtraWhere:

    def _conn(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE src (id INTEGER PRIMARY KEY, ts TEXT, archived INT)')
        conn.executemany(
            'INSERT INTO src (ts, archived) VALUES (?, ?)',
            [('2020-01-01', 1), ('2020-01-02', 0), ('2020-01-03', 1),
             ('2020-01-04', 0), ('2021-06-01', 0)],
        )
        conn.commit()
        return conn

    def test_only_matching_rows_deleted(self):
        conn = self._conn()
        deleted = _delete_older_than_chunked(
            conn, 'src', 'ts', '2021-01-01', extra_where='archived = 1')
        assert deleted == 2
        left = [r[0] for r in conn.execute('SELECT ts FROM src ORDER BY ts')]
        assert left == ['2020-01-02', '2020-01-04', '2021-06-01'], \
            '未归档的旧数据被删掉了 —— 迟到回填的数据会这样永久消失'

    def test_without_extra_where_deletes_all_older(self):
        """不带条件时行为不变（其它调用方依赖它）。"""
        conn = self._conn()
        deleted = _delete_older_than_chunked(conn, 'src', 'ts', '2021-01-01')
        assert deleted == 4
        left = [r[0] for r in conn.execute('SELECT ts FROM src')]
        assert left == ['2021-06-01']

    def test_rejects_bad_identifier(self):
        conn = self._conn()
        with pytest.raises(ValueError):
            _delete_older_than_chunked(conn, 'src; DROP TABLE src', 'ts', '2021-01-01')


class TestArchiveDeleteUsesArchiveGuard:

    def test_archive_old_data_passes_exists_condition(self):
        """删除时必须带「归档表里存在对应日聚合」这个条件。

        走源码静态断言而不是跑真库：这条要防的是「哪天有人把 extra_where 删了」，
        不需要真跑一次归档 —— 真跑还得造几十天数据，慢且脆。
        """
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / '存储层' / 'database.py'
        text = src.read_text(encoding='utf-8')

        idx = text.find("deleted_rows = _delete_older_than_chunked(")
        assert idx > 0, 'archive_old_data 里找不到删除调用'
        call = text[idx:idx + 400]
        assert 'extra_where=archived_only' in call, \
            '删除历史数据时没传归档校验条件 —— 迟到回填的旧数据会被直接删掉'

        cond_start = text.find('archived_only = (')
        assert cond_start > 0, '找不到 archived_only 条件定义'
        cond = text[cond_start:cond_start + 400]
        assert 'EXISTS' in cond.upper()
        assert '{ARCHIVE_TABLE}' in cond, '条件里必须用归档表常量，不能写死别的表名'
        assert 'DATE(history_data.timestamp)' in cond
