"""保留策略时间戳格式回归测试。

背景（2026-09 审计发现的真实数据丢失缺陷）：
库内 timestamp 由 sqlite3 适配器写成 **空格分隔** 的 `'YYYY-MM-DD HH:MM:SS.ffffff'`，
但清理/归档/范围查询用 `datetime.isoformat()` 生成 cutoff，得到 **'T' 分隔** 的
`'YYYY-MM-DDTHH:MM:SS'`。SQLite 是按文本比较的，ASCII 中 `'T'`(0x54) > `' '`(0x20)，
于是 `'2026-08-17 23:59:59' < '2026-08-17T10:00:00'` 成立 ——
**同一天里比 cutoff 更晚的记录也会被判定为「过旧」而删除**。

后果：每次清理多删近一天数据；黑名单清理同理，会削弱令牌撤销时效。

本测试用「cutoff 当天稍晚的记录必须存活」来锁死这个行为。
"""
from datetime import datetime, timedelta

import pytest

from 存储层.data_lifecycle import DataLifecycleManager, RetentionPolicy


@pytest.fixture
def lifecycle_db(tmp_path):
    """建一个只有 history_data 的最小库，返回 (manager, db_path)"""
    import sqlite3
    db_path = tmp_path / "retention.db"
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE history_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            timestamp TEXT,
            value REAL
        )
    ''')
    conn.commit()
    conn.close()

    mgr = DataLifecycleManager(str(db_path))
    # 只保留 history_data，关闭归档以隔离"删除"路径
    mgr.policies = {
        'history_data': RetentionPolicy(
            name='history_data', table='history_data',
            retention_days=30, archive_enabled=False,
        )
    }
    return mgr, db_path


def _insert(db_path, rows):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO history_data (device_id, timestamp, value) VALUES (?, ?, ?)", rows
    )
    conn.commit()
    conn.close()


def _count(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0]
    conn.close()
    return n


def test_same_day_record_after_cutoff_survives(lifecycle_db):
    """cutoff 当天、但比 cutoff 时刻更晚的记录，不能被删。

    这正是 'T' vs ' ' 格式不一致会踩中的边界：字符串比较会把整天的记录都判为过旧。
    """
    mgr, db_path = lifecycle_db
    cutoff = datetime.now() - timedelta(days=30)
    # 与 cutoff 同一日历日，但时间更晚（23:59:59 > cutoff 的时刻）
    same_day_later = cutoff.replace(hour=23, minute=59, second=59, microsecond=0)

    _insert(db_path, [
        ('dev1', same_day_later.strftime('%Y-%m-%d %H:%M:%S'), 1.0),
    ])

    mgr.execute_lifecycle()

    assert _count(db_path) == 1, (
        f'cutoff={cutoff} 当天更晚的记录 {same_day_later} 被误删了 —— '
        '时间戳格式不一致（isoformat 的 T 分隔 vs 库内空格分隔）'
    )


def test_older_record_is_deleted(lifecycle_db):
    """真正过期的记录必须被删掉（确认清理功能本身没被改坏）"""
    mgr, db_path = lifecycle_db
    cutoff = datetime.now() - timedelta(days=30)
    # 早于 cutoff 一整天，且落在不同日历日
    clearly_old = (cutoff - timedelta(days=1)).replace(microsecond=0)

    _insert(db_path, [
        ('dev1', clearly_old.strftime('%Y-%m-%d %H:%M:%S'), 1.0),
    ])

    mgr.execute_lifecycle()

    assert _count(db_path) == 0, '明显过期的记录应当被删除'


def test_cutoff_format_matches_db_storage_format():
    """cutoff 的字符串格式必须与库内写入格式一致（不变式守卫）。

    库内写入走 存储层.database.adapt_datetime -> isoformat(sep=' ')，
    任何用于比较的 cutoff 都必须产出同格式字符串。
    """
    from 存储层.database import adapt_datetime

    now = datetime(2026, 8, 17, 10, 30, 0)
    db_format = adapt_datetime(now)

    assert 'T' not in db_format, '库内写入格式不应含 T 分隔符'
    assert db_format == '2026-08-17 10:30:00'

    # 用于比较的 cutoff 也必须同格式
    cutoff = now.isoformat(sep=' ')
    assert cutoff == db_format, 'cutoff 与库内格式不一致会导致文本比较错判'

    # 反向验证：T 分隔确实会让"同天更晚"被误判为更早
    buggy = now.isoformat()
    later_same_day = '2026-08-17 23:00:00'
    assert later_same_day < buggy, (
        '前提失效：若此断言不成立，说明本测试要防的 bug 已不适用，请复核'
    )
    assert not (later_same_day < cutoff), '正确格式下同天更晚的记录不应被判为更早'
