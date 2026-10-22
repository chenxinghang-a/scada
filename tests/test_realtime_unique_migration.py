"""
`realtime_data` 唯一约束迁移（round 246）

缺陷
----
建表语句里写着 `UNIQUE(device_id, register_name)`，但
`CREATE TABLE IF NOT EXISTS` 遇到**已存在的老表**直接跳过 ——
而 SQLite **不支持** `ALTER TABLE ... ADD CONSTRAINT`。
所以老库永远拿不到这个约束。

后果不是报错，是**静默退化**：
`insert_data_batch` 里的 `INSERT ... ON CONFLICT(device_id, register_name)
DO UPDATE` 找不到冲突目标 → 每次都 INSERT 新行。

实测 `data/scada.db`（旧 schema）：

    迁移前：239,815 行 / 166 个点位 / 无唯一索引
            单点位最多 13,929 行
    迁移后：166 行 / ux_realtime_point 已建
    连写 3 次同一点位 → 1 行，值 = 3.0（UPSERT 终于生效）

而 `cleanup_old_data` 的注释一直写着「realtime_data 有 UNIQUE 且走 UPSERT，
行的数量上界 = 点位总数，根本不会随时间无界增长」—— 恰好是反的，
于是这张表**从来不被清理**，一路膨胀到 495 MB 库里的大部分。

修法照搬归档表（`idx_archive_unique`，round 229）的成熟做法：
先删重复（每个点位保留 id 最大的一行 = 最新），再建唯一索引。
幂等、可反复执行；失败只记 ERROR 不阻塞启动（服务照常可用，下次启动重试）。
"""

import os
import sqlite3
import sys
from datetime import datetime

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from 存储层.database import Database  # noqa: E402

LEGACY_DDL = '''
CREATE TABLE realtime_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    register_name TEXT NOT NULL,
    value REAL,
    unit TEXT,
    timestamp DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
'''


def _make_legacy_db(path):
    """造一个旧 schema 的库：realtime_data 无 UNIQUE，且有重复行。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_DDL)
    # D1/t 写了 5 次（value 0..4），D1/p 只写 1 次
    for i in range(5):
        conn.execute(
            "INSERT INTO realtime_data (device_id, register_name, value, unit, timestamp) "
            "VALUES ('D1','t',?,'', '2026-01-01 00:00:00')", (float(i),))
    conn.execute(
        "INSERT INTO realtime_data (device_id, register_name, value, unit, timestamp) "
        "VALUES ('D1','p',9.0,'', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()


def _rows(path):
    with sqlite3.connect(str(path)) as conn:
        return conn.execute(
            "SELECT register_name, value FROM realtime_data ORDER BY register_name").fetchall()


def _unique_indexes(path):
    with sqlite3.connect(str(path)) as conn:
        return [r[1] for r in conn.execute("PRAGMA index_list(realtime_data)") if r[2]]


# ============================================================
# 1. 老库会被迁移
# ============================================================

class TestLegacyMigration:
    def test_unique_index_is_created(self, tmp_path):
        p = tmp_path / "legacy.db"
        _make_legacy_db(p)
        assert _unique_indexes(p) == []

        Database(str(p)).close()

        assert _unique_indexes(p), '迁移没有补上唯一约束'

    def test_duplicates_are_removed_keeping_latest(self, tmp_path):
        p = tmp_path / "legacy.db"
        _make_legacy_db(p)
        assert len(_rows(p)) == 6

        Database(str(p)).close()

        rows = _rows(p)
        assert len(rows) == 2, f'重复行没删干净: {rows}'
        # 每个点位保留 id 最大（最新）的一行 → t 是 4.0
        assert rows == [('p', 9.0), ('t', 4.0)]

    def test_upsert_works_after_migration(self, tmp_path):
        p = tmp_path / "legacy.db"
        _make_legacy_db(p)
        db = Database(str(p))

        now = datetime.now()
        for v in (1.0, 2.0, 3.0):
            db.insert_data_batch([{'device_id': 'D1', 'register_name': 't',
                                   'value': v, 'unit': '', 'timestamp': now}])
        db.close()

        rows = [r for r in _rows(p) if r[0] == 't']
        assert len(rows) == 1, f'UPSERT 仍然在插新行: {rows}'
        assert rows[0][1] == 3.0

    def test_migration_is_idempotent(self, tmp_path):
        p = tmp_path / "legacy.db"
        _make_legacy_db(p)

        Database(str(p)).close()
        first = _rows(p)

        Database(str(p)).close()   # 第二次启动
        second = _rows(p)

        assert first == second, '第二次迁移改动了数据'
        assert len(_unique_indexes(p)) >= 1


# ============================================================
# 2. 新库不受影响
# ============================================================

class TestFreshDb:
    def test_fresh_db_has_unique_constraint(self, tmp_path):
        p = tmp_path / "fresh.db"
        db = Database(str(p))
        db.close()
        assert _unique_indexes(p), '新建库没有唯一约束'

    def test_fresh_db_upsert_single_row(self, tmp_path):
        p = tmp_path / "fresh.db"
        db = Database(str(p))
        now = datetime.now()
        for v in (10.0, 20.0):
            db.insert_data_batch([{'device_id': 'D9', 'register_name': 'x',
                                   'value': v, 'unit': '', 'timestamp': now}])
        db.close()

        rows = [r for r in _rows(p) if r[0] == 'x']
        assert len(rows) == 1
        assert rows[0][1] == 20.0

    def test_fresh_db_migration_does_not_delete(self, tmp_path):
        """新库本来就没重复，迁移不该误删。"""
        p = tmp_path / "fresh.db"
        db = Database(str(p))
        now = datetime.now()
        for reg in ('a', 'b', 'c'):
            db.insert_data_batch([{'device_id': 'D9', 'register_name': reg,
                                   'value': 1.0, 'unit': '', 'timestamp': now}])
        db.close()

        Database(str(p)).close()

        assert len(_rows(p)) == 3


# ============================================================
# 3. 迁移失败不能阻塞启动
# ============================================================

def test_migration_failure_does_not_block_startup(tmp_path, caplog):
    """迁移失败只记 ERROR，服务必须照常起来（否则一次迁移问题就变成停机）。

    注意不能直接把 `_migrate_realtime_unique` 整个替换掉 —— 那样会连方法
    **内部**的 try/except 一起换走，异常直接冒到 `__init__`，测的就变成了
    「替换后的方法会不会抛」，与真实容错无关。这里只让 `CREATE UNIQUE INDEX`
    这一步失败，其余照常执行。
    """
    p = tmp_path / "legacy.db"
    _make_legacy_db(p)

    conn = sqlite3.connect(str(p))
    real_cursor = conn.cursor()

    class _FailingCursor:
        """真实 cursor 的包装：只在建唯一索引那步抛异常。"""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if 'CREATE UNIQUE INDEX' in sql:
                raise sqlite3.OperationalError('模拟索引创建失败')
            return self._real.execute(sql, *a, **kw)

        def fetchall(self):
            return self._real.fetchall()

        def fetchone(self):
            return self._real.fetchone()

    db = Database.__new__(Database)      # 跳过 __init__（会重建表、掩盖问题）
    db.db_path = str(p)

    with caplog.at_level('ERROR'):
        db._migrate_realtime_unique(_FailingCursor(real_cursor))   # 不抛即通过

    conn.close()

    assert any('realtime_data' in r.getMessage() for r in caplog.records), \
        '迁移失败没有留下任何 ERROR 痕迹'


# ============================================================
# 4. 静态守卫
# ============================================================

def test_migration_is_wired_into_init():
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '存储层' / 'database.py').read_text(encoding='utf-8')
    assert re.search(r'^\s+self\._migrate_realtime_unique\(cursor\)', source, flags=re.MULTILINE), \
        '_init_database 没有调用 _migrate_realtime_unique —— 老库永远拿不到唯一约束'


def test_migration_checks_existing_unique_index():
    """迁移必须先查 PRAGMA index_list，否则每次启动都会全表 DELETE。"""
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '存储层' / 'database.py').read_text(encoding='utf-8')
    body = source.split('def _migrate_realtime_unique', 1)[1].split('def ', 1)[0]

    assert 'PRAGMA index_list(realtime_data)' in body
    assert 'CREATE UNIQUE INDEX' in body
    assert 'GROUP BY device_id, register_name' in body
