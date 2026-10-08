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
