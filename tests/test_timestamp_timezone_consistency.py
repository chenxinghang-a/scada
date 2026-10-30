"""时间戳时区一致性测试（round 326）。

背景（真实缺陷，已在真实库上实测复现）
--------------------------------------
``data/scada.db`` 里**同一行数据**的两个时间字段相差 8 小时：

  - ``timestamp``   由 Python ``datetime.now()`` 写入 → **本地时间**（UTC+8）
  - ``created_at``  由 SQLite ``DEFAULT CURRENT_TIMESTAMP`` 写入 → **UTC**

实测 ``realtime_data`` / ``history_data`` / ``alarm_records`` 各取 5000 行样本，
``timestamp - created_at`` 的小时差值分布恒为 ``{8: 5000}``（无混存）。

后果
----
1. 任何 ``WHERE timestamp > created_at`` 的比较都错 8 小时；
2. 按 ``created_at`` 分天的报表，与按 ``timestamp`` 分天的口径差 8 小时；
3. ``python:3.12-slim`` 容器默认 UTC，而开发机是 UTC+8 —— 同一份代码在两个
   环境写出的时间戳属于**不同时区**，混存后无法区分（无法靠数据本身判断）。

修法
----
INSERT 时**显式写** ``created_at = datetime.now()``，不再依赖 DDL 默认值；
Dockerfile / docker-compose 钉死 ``TZ=Asia/Shanghai``。

为什么要有静态守卫
------------------
行为断言（「两个字段差 < 5 秒」）在 **UTC 机器上恒真** —— CI runner 就是 UTC，
那里 ``datetime.now()`` 与 ``CURRENT_TIMESTAMP`` 本来就相等，回归抓不到。
所以另加一条**不依赖运行时时钟**的源码扫描，任何时区都能拦住。
"""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from 存储层.database import Database

#: 允许的偏差：批量插入在同一个时刻取 created_at，逐条插入也只在毫秒级。
#: 8 小时的时区差远超这个阈值，所以能干净地区分「同尺度」与「差一个时区」。
MAX_SKEW_SECONDS = 60


@pytest.fixture()
def database(tmp_path):
    db_path = tmp_path / "tz_test.db"
    inst = Database(str(db_path))
    yield inst
    inst.close()


def _raw(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _skew_hours(ts, created_at):
    a = datetime.fromisoformat(str(ts))
    b = datetime.fromisoformat(str(created_at))
    return (a - b).total_seconds() / 3600.0


class TestCreatedAtMatchesTimestampScale:
    """``created_at`` 必须与 ``timestamp`` 同一时区尺度（都是本地时间）。"""

    def test_insert_data_single(self, database):
        ts = datetime.now()
        database.insert_data("dev1", "reg1", 42.0, ts, unit="kW")
        row = _raw(database.db_path,
                   "SELECT timestamp, created_at FROM realtime_data "
                   "WHERE device_id='dev1' AND register_name='reg1'")[0]
        skew = _skew_hours(row["timestamp"], row["created_at"])
        assert abs(skew) < MAX_SKEW_SECONDS / 3600, (
            f"created_at 与 timestamp 相差 {skew:.1f} 小时 —— "
            f"created_at 退回了 DDL 的 UTC 默认值")

    def test_insert_data_batch(self, database):
        ts = datetime.now()
        database.insert_data_batch([
            {"device_id": "dev2", "register_name": "reg2", "value": 1.5,
             "unit": "A", "timestamp": ts, "quality": 192},
        ])
        for table in ("realtime_data", "history_data"):
            rows = _raw(database.db_path,
                        f"SELECT timestamp, created_at FROM {table} "
                        f"WHERE device_id='dev2'")
            assert rows, f"{table} 没写进去"
            skew = _skew_hours(rows[0]["timestamp"], rows[0]["created_at"])
            assert abs(skew) < MAX_SKEW_SECONDS / 3600, (
                f"{table}: created_at 与 timestamp 相差 {skew:.1f} 小时")

    def test_insert_alarm(self, database):
        ts = datetime.now()
        database.insert_alarm("A1", "dev3", "reg3", "HIGH", "超温", 80.0, 95.0, ts)
        rows = _raw(database.db_path,
                    "SELECT timestamp, created_at FROM alarm_records WHERE alarm_id='A1'")
        assert rows, "报警没写进去"
        skew = _skew_hours(rows[0]["timestamp"], rows[0]["created_at"])
        assert abs(skew) < MAX_SKEW_SECONDS / 3600, (
            f"alarm_records: created_at 与 timestamp 相差 {skew:.1f} 小时")


class TestInsertStatementsExplicitlySetCreatedAt:
    """静态守卫：INSERT 语句必须显式列出 ``created_at``。

    这条**不依赖运行时时钟** —— 在 UTC 机器（CI）上，行为断言恒真，
    只有这条能拦住「有人把 created_at 从 INSERT 里删掉」的回归。
    """

    TABLES = ("realtime_data", "history_data", "alarm_records")

    @staticmethod
    def _code_without_comments() -> str:
        src = Path(__file__).resolve().parents[1] / "存储层" / "database.py"
        text = src.read_text(encoding="utf-8")
        # 去掉行注释，避免注释里出现的 created_at 字样造成误判（假绿）
        return re.sub(r"#[^\n]*", "", text)

    def test_every_insert_lists_created_at(self):
        code = self._code_without_comments()
        checked = 0
        for table in self.TABLES:
            stmts = re.findall(
                rf"INSERT\s+INTO\s+{table}\s*\((.*?)\)\s*VALUES", code, re.S)
            assert stmts, f"没找到 {table} 的 INSERT 语句 —— 扫描规则失效了"
            for cols in stmts:
                checked += 1
                assert "created_at" in cols, (
                    f"{table} 的 INSERT 没有显式写 created_at：\n{cols.strip()}\n"
                    f"会退回 DDL 的 UTC 默认值，与本地 timestamp 差 8 小时")
        assert checked >= 4, f"只检查了 {checked} 条 INSERT，少于预期（扫描可能失效）"
