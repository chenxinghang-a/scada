"""实时数据质量码持久化测试。

背景（这是一个真实的契约缺口，不是假设）：
    采集层 ``data_collector._handle_normal_collection`` 早就把 OPC UA 质量码
    算好了（``DataQualityAssessor``：192/104/0/4/6/8/80/64），但
    ``insert_data_batch`` 的 ``valid_rows`` 只取 5 个字段，**把它整个丢掉**；
    ``realtime_data`` 建表也没有 quality 列。结果前端 Dashboard 那套
    「质量圆点 + Good/Uncertain/Bad 三档映射」UI 永远拿不到数据，
    恒定走 ``quality == null`` 的降级分支 —— UI 是装饰品。

本文件锁死修复后的不变量，覆盖三条：
    1. 质量码能**原样落库**（不是被默认值覆盖）
    2. 未提供质量码时回落 GOOD(192)，且**不因值是字符串就静默存错**
    3. 旧库（无 quality 列）能被 ``_migrate_realtime_quality`` 就地补列

用词注意：这里的 "quality" 是 OPC UA 数值质量码（int），
**不是** OEE 里那个 0~1 的"质量率"。两者同名不同义，见 industry40.ts。
"""

import sqlite3
from datetime import datetime

import pytest

from 存储层.database import Database

# 与采集层 DataQualityAssessor 保持一致的常量（刻意在本文件重复声明，
# 而不是 import：这样采集层改了常量值、这里不会被"自动同步"掩盖，
# 反而会在断言处暴露出来，逼人回看契约是否还成立）
GOOD = 192
UNCERTAIN = 104
UNCERTAIN_LAST_USABLE = 64
BAD = 0
BAD_SENSOR_FAILURE = 4
BAD_COMM_FAILURE = 6


@pytest.fixture()
def database(tmp_path):
    """全新的空库（走 Database.__init__ → _init_database 建表路径）。"""
    db_path = tmp_path / "quality_test.db"
    inst = Database(str(db_path))
    yield inst
    inst.close()


def _row(database, device_id, register_name):
    """从 realtime_data 取单行（绕开 ORM 之外的一切包装，直查原始表）。"""
    conn = sqlite3.connect(database.db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT * FROM realtime_data WHERE device_id = ? AND register_name = ?",
            (device_id, register_name),
        )
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _batch(*items):
    return [
        {
            "device_id": d,
            "register_name": r,
            "value": v,
            "timestamp": datetime.now(),
            "unit": u,
            **({"quality": q} if q is not None else {}),
        }
        for (d, r, v, u, q) in items
    ]


class TestQualityColumnExists:
    """第 1 层：表结构本身有 quality 列。"""

    def test_realtime_data_has_quality_column(self, database):
        """realtime_data 必须有 quality 列 —— 没有它，链路第一步就断。"""
        conn = sqlite3.connect(database.db_path)
        try:
            cur = conn.execute("PRAGMA table_info(realtime_data)")
            cols = {row[1] for row in cur.fetchall()}
        finally:
            conn.close()
        assert "quality" in cols, f"realtime_data 缺 quality 列，实际列={sorted(cols)}"

    def test_quality_column_is_integer(self, database):
        """必须是 INTEGER（存的是 OPC UA 数值码，不是字符串标记）。"""
        conn = sqlite3.connect(database.db_path)
        try:
            cur = conn.execute("PRAGMA table_info(realtime_data)")
            info = {row[1]: row[2] for row in cur.fetchall()}
        finally:
            conn.close()
        assert info.get("quality", "").upper() == "INTEGER", (
            f"quality 列类型应为 INTEGER，实际={info.get('quality')!r}"
        )


class TestQualityPersisted:
    """第 2 层：写进去的质量码要能原样读出来。"""

    @pytest.mark.parametrize(
        "code",
        [GOOD, UNCERTAIN, UNCERTAIN_LAST_USABLE, BAD, BAD_SENSOR_FAILURE, BAD_COMM_FAILURE],
    )
    def test_each_opcua_code_round_trips(self, database, code):
        """六种典型 OPC UA 质量码逐一原样落库。

        参数化而非单条：**这是防"只对了 GOOD 一种"的假绿**。
        曾经的风险是代码里写 `quality or 192`（or 会把 0 当成假值替换掉），
        那样只有 BAD=0 会错、其他全过 —— 单条用例抓不到。
        """
        database.insert_data_batch(_batch(("dev", f"reg_{code}", 1.0, "u", code)))
        row = _row(database, "dev", f"reg_{code}")
        assert row is not None
        assert row["quality"] == code, (
            f"质量码 {code} 未原样落库，实际={row['quality']!r}"
            f"（若为 192 说明被默认值覆盖）"
        )

    def test_bad_zero_is_not_treated_as_missing(self, database):
        """**BAD=0 绝对不能因为"像假值"被替换成 192。**

        这是本组最关键的一条：0 是合法的、有明确含义的质量码（BAD）。
        任何 `d.get('quality') or 192` 写法都会把它吃掉，
        造成"数据质量明明是坏的，界面上却显示 Good"—— 安全语义上不可接受。
        """
        database.insert_data_batch(_batch(("dev", "bad_reg", 1.0, "u", BAD)))
        row = _row(database, "dev", "bad_reg")
        assert row["quality"] == BAD, (
            f"BAD(0) 被替换成了 {row['quality']!r} —— "
            f"检查是否写了 `quality or 192` 这类 falsy 兜底"
        )

    def test_upsert_updates_quality(self, database):
        """同一设备+寄存器二次写入时，quality 必须被 UPSERT 更新。

        只更新 value 不更新 quality 的话，前一秒的 BAD 会永久留在库里，
        设备恢复后界面仍然一直显示红色。
        """
        database.insert_data_batch(_batch(("dev", "reg", 1.0, "u", BAD)))
        assert _row(database, "dev", "reg")["quality"] == BAD

        database.insert_data_batch(_batch(("dev", "reg", 2.0, "u", GOOD)))
        row = _row(database, "dev", "reg")
        assert row["value"] == 2.0
        assert row["quality"] == GOOD, (
            f"UPSERT 没有更新 quality，仍为 {row['quality']!r}（期望 {GOOD}）"
        )

    def test_realtime_api_query_returns_quality(self, database):
        """走 ``get_realtime_data``（即 /api/data/realtime 的数据源）也要带上。

        直查表对了但查询层漏字段，前端照样收不到 —— 必须端到端断言一次。
        """
        database.insert_data_batch(_batch(("dev", "reg", 1.0, "u", UNCERTAIN)))
        rows = database.get_realtime_data("dev")
        assert rows, "get_realtime_data 未返回任何行"
        assert "quality" in rows[0], (
            f"get_realtime_data 的行里没有 quality 字段，实际键={sorted(rows[0])}"
        )
        assert rows[0]["quality"] == UNCERTAIN


class TestQualityDefaults:
    """第 3 层：缺省与非法值的处理。"""

    def test_missing_quality_defaults_to_good(self, database):
        """调用方没给质量码 → 默认 192(GOOD)。

        取"好"而不是"坏"是刻意的：默认给 BAD 会让所有老代码路径/手工灌数
        立刻报一片红，运维会被噪声淹没而忽略真正的坏点。
        """
        database.insert_data_batch(_batch(("dev", "reg", 1.0, "u", None)))
        assert _row(database, "dev", "reg")["quality"] == GOOD

    def test_numeric_string_is_coerced(self, database):
        """数字字符串（如从 JSON 反序列化来的 '192'）应被接受并转成 int。

        容忍它是为了兼容外部灌数/网关转发的场景。
        """
        database.insert_data_batch(_batch(("dev", "reg", 1.0, "u", "104")))
        row = _row(database, "dev", "reg")
        assert row["quality"] == UNCERTAIN
        assert isinstance(row["quality"], int), (
            "字符串形式的数字必须转成 int 存库，否则前端数值比较会出错"
        )

    def test_non_numeric_marker_falls_back_to_good(self, database):
        """非数值标记（'BAD'/'good'/'simulated'）不能存进库。

        存进去的话前端 `q >= 192` 会对字符串做比较 ——
        JS 里 `'BAD' >= 192` 为 false，静默判成 Bad 且不报错，
        属于最难查的一类 bug。这里统一回落 192。
        """
        database.insert_data_batch(_batch(("dev", "reg", 1.0, "u", "BAD")))
        row = _row(database, "dev", "reg")
        assert row["quality"] == GOOD, (
            f"非数值标记应回落 {GOOD}，实际={row['quality']!r}"
            f"（若存成字符串说明缺少 int() 收敛）"
        )
        assert isinstance(row["quality"], int)

    def test_invalid_record_still_skipped(self, database):
        """回归保护：缺必需字段的记录仍被跳过，不影响同批其他记录。"""
        batch = _batch(("dev", "ok", 1.0, "u", GOOD))
        batch.append({"device_id": "dev", "value": 9.0})  # 缺 register_name/timestamp
        database.insert_data_batch(batch)

        assert _row(database, "dev", "ok") is not None, "合法记录被连带丢弃了"
        assert _row(database, "dev", "ok")["quality"] == GOOD


class TestMigrationFromLegacyDb:
    """第 4 层：旧库（建表时无 quality 列）必须能就地补列。

    **为什么单独测**：``CREATE TABLE IF NOT EXISTS`` 对已存在的表是空操作，
    所以光改建表语句**对已有生产库完全无效** —— 必须靠 ALTER TABLE 迁移。
    如果迁移缺失，旧库上所有写库操作会因"表无此列"直接抛异常，
    而新库上一切正常 → 典型的"只在生产上炸"。
    """

    def test_legacy_table_gets_quality_added(self, tmp_path):
        """手工造一个没有 quality 列的旧库，验证迁移把它补上。"""
        db_path = tmp_path / "legacy.db"
        # 1) 用原生 sqlite 造旧表结构（模拟 1.3.1032 之前的库）
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE realtime_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                register_name TEXT NOT NULL,
                value REAL,
                unit TEXT,
                timestamp DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(device_id, register_name)
            )
            """
        )
        conn.execute(
            "INSERT INTO realtime_data (device_id, register_name, value, timestamp) "
            "VALUES ('old_dev', 'old_reg', 1.5, '2026-01-01 00:00:00')"
        )
        conn.commit()

        # 迁移前确认真的没有该列（否则本用例测的是空气）
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(realtime_data)")}
        conn.close()
        assert "quality" not in cols_before, (
            "测试前提不成立：手工建的旧表里已经有 quality 列了"
        )

        # 2) 打开 Database → 应触发 _migrate_realtime_quality
        inst = Database(str(db_path))
        try:
            conn2 = sqlite3.connect(str(db_path))
            conn2.row_factory = sqlite3.Row
            cols_after = {r[1] for r in conn2.execute("PRAGMA table_info(realtime_data)")}
            assert "quality" in cols_after, (
                f"迁移未执行 —— 旧库仍无 quality 列，实际={sorted(cols_after)}"
            )

            # 3) 旧的存量行应得到默认值 192（GOOD）：
            #    已存在的行无法追溯真实质量，给 GOOD 是保守选择 ——
            #    宁可显示"好"也不要凭空报"坏"（后者会引发无谓告警）。
            old = conn2.execute(
                "SELECT * FROM realtime_data WHERE register_name = 'old_reg'"
            ).fetchone()
            conn2.close()
            assert old is not None, "旧数据行丢失"
            assert old["quality"] == GOOD, (
                f"存量行 quality 应为默认 {GOOD}，实际={old['quality']!r}"
            )
        finally:
            inst.close()

    def test_writes_work_after_migration(self, tmp_path):
        """迁移后必须能正常写库（证明补的列真的可用，不是摆设）。"""
        db_path = tmp_path / "legacy2.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE realtime_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                register_name TEXT NOT NULL,
                value REAL,
                unit TEXT,
                timestamp DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(device_id, register_name)
            )
            """
        )
        conn.commit()
        conn.close()

        inst = Database(str(db_path))
        try:
            inst.insert_data_batch(_batch(("new_dev", "new_reg", 7.0, "u", BAD)))
        finally:
            inst.close()

        assert _row_by_path(db_path, "new_dev", "new_reg")["quality"] == BAD


def _row_by_path(db_path, device_id, register_name):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT * FROM realtime_data WHERE device_id = ? AND register_name = ?",
            (device_id, register_name),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()
