# -*- coding: utf-8 -*-
"""备份恢复演练（round 273）：每天真的验一次"备份能不能用"。

缺陷形态（不是 bug，是**运维缺口**）
-----------------------------------
`backup_database()` 很早就有，`restore_database()` 是 round 261 才补的 —— 但两者
都只在**被调用时**执行。生产上没人每天去点一次"恢复"，于是三种情况只在
**真要恢复的那天**才暴露：

1. 备份文件是坏的（备份时磁盘满、进程被杀、只写了半截）；
2. 备份**早就停了**（定时任务挂了、路径变了），最新可用备份是三个月前的；
3. 备份的是空库 / 别的库（行数远小于生产库）。

修法：每日演练 —— 找最新备份 → **只读**打开 → `integrity_check` + 必需表 +
行数对比 + 新鲜度 → 失败报警、恢复自动清除。

为什么不真的恢复一遍：每天复制 500MB 库是不必要的 IO。`integrity_check`
能发现截断/坏页，行数与新鲜度能发现"空库/停摆"，真正的恢复路径由
`tests/test_backup_restore_drill.py` 每次 CI 验证。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest


def _make_db(path: Path, *, rows: int = 5):
    """造一个最小的合法库（含三张必需表）。"""
    from 存储层.database import Database
    db = Database(str(path))
    ts = datetime(2026, 10, 26, 0, 0, 0)
    for i in range(rows):
        db.insert_data('dev1', f'r{i}', float(i), ts)
        db.insert_data_batch([{'device_id': 'dev1', 'register_name': 'temp',
                               'value': float(i), 'timestamp': ts}])
    db.insert_alarm('r1', 'dev1', 'temp', 'warning', '测试', 1.0, 2.0, ts)
    db.close()
    return path


def _backup(db_path: Path, backup_dir: Path, name: str = 'scada_20261026_000000.db'):
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / name
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


@pytest.fixture
def env(tmp_path):
    db_path = _make_db(tmp_path / 'prod.db', rows=10)
    backup_dir = tmp_path / 'backups'
    backup = _backup(db_path, backup_dir)
    from 存储层.database import Database
    live = Database(str(db_path))
    yield {'db': live, 'dir': backup_dir, 'backup': backup, 'db_path': db_path}
    live.close()


# ---------------------------------------------------------------------------
# 1. 正常路径
# ---------------------------------------------------------------------------

def test_drill_passes_on_healthy_backup(env):
    from core.backup_drill import run_drill

    result = run_drill(database=env['db'], backup_dir=str(env['dir']))
    assert result['ok'], result['problems']
    assert result['backup'].endswith('.db')
    assert result['detail']['integrity'] == 'ok'
    assert result['detail']['rows']['alarm_records'] >= 1


def test_drill_reports_backup_age(env):
    from core.backup_drill import run_drill

    result = run_drill(database=env['db'], backup_dir=str(env['dir']))
    assert result['age_hours'] is not None
    assert result['age_hours'] < 1.0


# ---------------------------------------------------------------------------
# 2. 失败路径
# ---------------------------------------------------------------------------

def test_missing_backup_dir_fails(tmp_path):
    from core.backup_drill import run_drill

    result = run_drill(database=None, backup_dir=str(tmp_path / 'nope'))
    assert not result['ok']
    assert any('没有任何' in p for p in result['problems'])


def test_empty_backup_fails(env):
    from core.backup_drill import run_drill

    (env['dir'] / 'broken.db').write_bytes(b'')
    # 让空文件成为"最新"
    import os
    os.utime(env['dir'] / 'broken.db', (time.time(), time.time()))

    result = run_drill(database=env['db'], backup_dir=str(env['dir']))
    assert not result['ok']
    assert any('0 字节' in p for p in result['problems'])


def test_corrupt_backup_fails(env):
    from core.backup_drill import run_drill

    corrupt = env['dir'] / 'corrupt.db'
    corrupt.write_bytes(b'not a sqlite file' * 100)
    import os
    os.utime(corrupt, (time.time(), time.time()))

    result = run_drill(database=env['db'], backup_dir=str(env['dir']))
    assert not result['ok']
    assert result['problems']


def test_backup_without_required_tables_fails(tmp_path):
    from core.backup_drill import run_drill

    other = tmp_path / 'other.db'
    conn = sqlite3.connect(str(other))
    conn.execute('CREATE TABLE unrelated (id INTEGER)')
    conn.commit()
    conn.close()

    result = run_drill(database=None, backup_dir=str(tmp_path))
    assert not result['ok']
    assert any('缺少必需的表' in p for p in result['problems'])


def test_stale_backup_fails(env):
    from core.backup_drill import run_drill, STALE_BACKUP_HOURS

    old = time.time() - (STALE_BACKUP_HOURS + 5) * 3600
    import os
    os.utime(env['backup'], (old, old))

    result = run_drill(database=env['db'], backup_dir=str(env['dir']))
    assert not result['ok']
    assert any('定时备份可能已经停了' in p for p in result['problems'])


def test_backup_much_smaller_than_live_fails(env):
    """备份行数不足生产库一半 → 备份不完整（备份的是旧库/空库）。"""
    from core.backup_drill import run_drill

    # 备份之后往生产库灌大量数据
    ts = datetime(2026, 10, 26, 1, 0, 0)
    env['db'].insert_data_batch([
        {'device_id': 'dev1', 'register_name': 'temp', 'value': float(i), 'timestamp': ts}
        for i in range(500)
    ])

    result = run_drill(database=env['db'], backup_dir=str(env['dir']))
    assert not result['ok'], '备份只有生产库的一小部分，却没被判为不完整'
    assert any('不足一半' in p for p in result['problems'])


# ---------------------------------------------------------------------------
# 3. 报警接线
# ---------------------------------------------------------------------------

class _FakeAlarm:
    def __init__(self):
        self.raised = []
        self.cleared = []

    def raise_external_alarm(self, **kw):
        self.raised.append(kw)
        return True

    def clear_external_alarm(self, alarm_id, device_id=None, register_name=None):
        self.cleared.append(alarm_id)
        return True


def test_failure_raises_alarm(env):
    from core.backup_drill import run_drill

    alarm = _FakeAlarm()
    run_drill(database=None, backup_dir=str(env['dir'] / 'missing'), alarm_manager=alarm)

    assert alarm.raised, '备份不可用却没有报警'
    assert alarm.raised[0]['alarm_id'] == 'backup_drill_failed'
    assert alarm.raised[0]['level'] == 'critical'


def test_success_clears_previous_alarm(env):
    from core.backup_drill import run_drill

    alarm = _FakeAlarm()
    run_drill(database=env['db'], backup_dir=str(env['dir']), alarm_manager=alarm)
    assert 'backup_drill_failed' in alarm.cleared


def test_drill_never_raises_on_broken_alarm_manager(env):
    """演练自己不能炸掉维护线程。"""
    from core.backup_drill import run_drill

    class _Boom:
        def raise_external_alarm(self, **kw):
            raise RuntimeError('boom')

        def clear_external_alarm(self, *a, **kw):
            raise RuntimeError('boom')

    result = run_drill(database=None, backup_dir=str(env['dir'] / 'missing'),
                       alarm_manager=_Boom())
    assert not result['ok']          # 结果仍要如实返回


# ---------------------------------------------------------------------------
# 4. 维护任务接线
# ---------------------------------------------------------------------------

def test_task_registered_with_database(env):
    from core.maintenance import build_maintenance_tasks, DEFAULT_INTERVALS

    names = [t[0] for t in build_maintenance_tasks(database=env['db'])]
    assert 'backup_restore_drill' in names
    assert DEFAULT_INTERVALS['backup_restore_drill'] == 86400


def test_task_runs_and_returns_summary(env):
    from core.maintenance import build_maintenance_tasks

    tasks = {t[0]: t[3] for t in build_maintenance_tasks(database=env['db'])}
    # 默认目录没有备份 → 失败结果，但不能抛异常
    result = tasks['backup_restore_drill']()
    assert 'ok' in result and 'problems' in result
