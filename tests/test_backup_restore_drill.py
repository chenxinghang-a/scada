# -*- coding: utf-8 -*-
"""备份 / 恢复演练（round 261）。

为什么要有这个文件
------------------
"有备份"和"能恢复"是两件事，中间隔着一个**演练**：

* 备份文件可能是坏的（备份时磁盘满、进程被杀）—— 没人验证过；
* WAL 模式下只拷 `.db`、留下旧的 `-wal`，SQLite 会把旧日志应用到新文件上，
  库直接损坏；
* 直接覆盖正在使用的库，替换到一半失败就两头空。

本项目在 round 261 之前**只有 `backup_database()`，没有任何恢复路径** ——
现场出事后只能手工拷文件。这个文件把"恢复"变成一条可重复执行、
可验证的路径：备份 → 破坏原库 → 恢复 → 逐表核对行数与内容。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def db(tmp_path):
    from 存储层.database import Database
    database = Database(str(tmp_path / 'drill.db'))
    yield database
    database.close()


def _seed(db):
    """写入可核对的样本：实时值、历史、报警、用户。"""
    ts = datetime(2026, 10, 25, 9, 0, 0)
    db.insert_data('dev1', 'temp', 88.5, ts, unit='℃')
    db.insert_data_batch([
        {'device_id': 'dev1', 'register_name': 'temp', 'value': 89.0,
         'timestamp': ts + timedelta(seconds=5), 'unit': '℃'},
        {'device_id': 'dev1', 'register_name': 'pressure', 'value': 1.05,
         'timestamp': ts + timedelta(seconds=5), 'unit': 'MPa'},
    ])
    db.insert_alarm('r1', 'dev1', 'temp', 'critical', '温度过高', 80.0, 88.5, ts)
    return ts


def _counts(db):
    return db.get_table_sizes()


def test_backup_then_restore_recovers_every_row(db, tmp_path):
    """完整演练：写数据 → 备份 → 删库 → 恢复 → 逐表行数一致。"""
    _seed(db)
    before = _counts(db)
    assert before['history_data'] >= 2 and before['alarm_records'] >= 1

    backup = db.backup_database(str(tmp_path / 'bk'))
    assert backup and str(backup).endswith('.db')

    # 破坏现场：关连接、删库（模拟磁盘故障/误删）
    db_path = db.db_path
    db.close()
    import os
    for suffix in ('', '-wal', '-shm'):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)

    # 恢复
    from 存储层.database import Database
    fresh = Database.__new__(Database)          # 不初始化，只借用恢复方法
    fresh.db_path = db_path
    ok, msg = fresh.restore_database(backup)
    assert ok, f'恢复失败: {msg}'

    restored = Database(db_path)
    try:
        after = _counts(restored)
        assert after == before, f'恢复后行数不一致：{before} → {after}'

        # 内容也要对，不只是行数
        records = restored.get_alarm_records(limit=10)
        assert records and records[0]['alarm_message'] == '温度过高'
        realtime = {r['register_name']: r['value']
                    for r in restored.get_realtime_data('dev1')}
        # realtime_data 是 UPSERT：后写的 89.0 覆盖了先写的 88.5 —— 恢复后必须
        # 保留"最新那条"，而不是"最早那条"
        assert realtime['temp'] == pytest.approx(89.0)
    finally:
        restored.close()


def test_restore_rejects_missing_or_empty_backup(db, tmp_path):
    ok, msg = db.restore_database(str(tmp_path / 'nope.db'))
    assert not ok and '不存在' in msg

    empty = tmp_path / 'empty.db'
    empty.write_bytes(b'')
    ok, msg = db.restore_database(str(empty))
    assert not ok and '空' in msg


def test_restore_rejects_non_database_file(db, tmp_path):
    junk = tmp_path / 'junk.db'
    junk.write_bytes(b'this is not a sqlite file' * 10)
    ok, msg = db.restore_database(str(junk))
    assert not ok, '非 SQLite 文件被当成备份接受了'
    assert '读取' in msg or '完整性' in msg


def test_restore_rejects_db_without_required_tables(db, tmp_path):
    """结构不对的库（比如别的应用的 .db）不许覆盖当前库。"""
    other = tmp_path / 'other.db'
    conn = sqlite3.connect(str(other))
    conn.execute('CREATE TABLE something_else (id INTEGER)')
    conn.commit()
    conn.close()

    ok, msg = db.restore_database(str(other))
    assert not ok and '缺少必需的表' in msg


def test_restore_keeps_safety_copy(db, tmp_path):
    """恢复前必须把当前库另存一份（新备份可能是更早但更糟的状态）。"""
    _seed(db)
    backup = db.backup_database(str(tmp_path / 'bk'))
    db.close()

    from 存储层.database import Database
    fresh = Database.__new__(Database)
    fresh.db_path = db.db_path
    ok, _ = fresh.restore_database(backup, safety_copy=True)
    assert ok

    import glob
    copies = glob.glob(str(tmp_path / '*.pre-restore-*.db'))
    assert copies, '没有生成恢复前副本 —— 恢复错了就再也回不去'


def test_restore_clears_stale_wal(db, tmp_path):
    """恢复后必须清掉旧 WAL/SHM，否则旧日志会被应用到新文件上。"""
    _seed(db)
    backup = db.backup_database(str(tmp_path / 'bk'))
    db.close()

    # 手工造一对残留的 -wal/-shm
    import pathlib
    for suffix in ('-wal', '-shm'):
        pathlib.Path(db.db_path + suffix).write_bytes(b'stale')

    from 存储层.database import Database
    fresh = Database.__new__(Database)
    fresh.db_path = db.db_path
    ok, _ = fresh.restore_database(backup)
    assert ok
    assert not pathlib.Path(db.db_path + '-wal').exists(), '残留 -wal 没被清理'
    assert not pathlib.Path(db.db_path + '-shm').exists(), '残留 -shm 没被清理'


def test_backup_is_consistent_snapshot(db, tmp_path):
    """备份出来的文件必须自洽：独立打开能通过完整性校验、能查到同样行数。"""
    _seed(db)
    backup = db.backup_database(str(tmp_path / 'bk'))

    conn = sqlite3.connect(f'file:{pathlib_posix(backup)}?mode=ro', uri=True)
    try:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        n = conn.execute('SELECT COUNT(*) FROM history_data').fetchone()[0]
        assert n == _counts(db)['history_data']
    finally:
        conn.close()


def pathlib_posix(p) -> str:
    from pathlib import Path
    return Path(str(p)).as_posix()
