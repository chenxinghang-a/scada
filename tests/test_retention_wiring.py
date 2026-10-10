# -*- coding: utf-8 -*-
"""数据保留策略的**接线**与**行为**守卫（round 209）。

缺陷类别：**声明了、有实现、有测试，但生产路径零调用**
====================================================
`配置/system.yaml` 声明了保留策略（历史 30 / 报警 90 / 归档 365 天），
`存储层/database.py` 也有实现与测试 —— 但**生产路径上从来没有人调用它，
全仓也没有任何调度器**。

实测（2026-10-10，只读查 `data/scada_simulated.db`，679 MB）：

| 表 | 行数 | 最早 | 最晚 | 按配置应删 |
|---|---|---|---|---|
| `history_data` | 1,991,423 | 2026-09-18 | 2026-10-09 | 0 行（还没到 30 天） |
| `alarm_records` | **105,028** | **2026-05-08** | **2026-05-31** | **105,028 = 100%** |

**10.5 万条报警全部是 5 月的（约 150 天前），配置说只留 90 天却一行没删。**
增速约 34 MB/天 ≈ 12 GB/年，而系统里有 `disk` 健康检查。

本文件钉三件事：
1. **接线**：调度器被 `run.py` 起、`enforce_retention_policy` 有**生产**调用点
   （不是只有测试调）、配置里有 `enabled`、手动清理端点的默认值取自配置；
2. **行为**：按窗口删、窗口内的留着、**绝不碰 `realtime_data`**；
3. **fail-safe**：配置缺失 / 未启用 / 数值非法时**什么都不删**
   （删数据不可逆，宁可「因为读不到所以没删」，也不要「因为读不到所以按默认值删」）。
"""

from __future__ import annotations

import os
import re
import sqlite3
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent

#: 生产代码目录（**排除 tests/** —— 只在测试里被调用不算「生产路径有人调」）。
_PROD_SKIP_DIRS = {"tests", ".venv", "dist", "build", "node_modules", "__pycache__",
                   "配置", "docs", ".git", "instances"}


def _iter_prod_py():
    for dirpath, dirnames, filenames in os.walk(BACKEND_ROOT):
        dirnames[:] = [
            d for d in dirnames
            if d not in _PROD_SKIP_DIRS and not d.startswith((".pytest_tmp", "dist-", "build-"))
        ]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def _strip_comments(src: str) -> str:
    """剥掉 `#` 注释。

    ⚠️ 必须先剥 —— `core/index_bootstrap.py` 里就有两行**注释**写着
    `enforce_retention_policy`（那正是「有实现没人调」的旁证），
    不剥注释的扫描会把它当成调用点，守卫立刻变瞎。
    """
    return re.sub(r"#.*$", "", src, flags=re.M)


# ---------------------------------------------------------------------------
# 1. 接线
# ---------------------------------------------------------------------------

class TestWiring:
    def test_retention_is_called_from_production(self):
        """`enforce_retention_policy` 必须有**生产**调用点（属性调用，不是 def）。"""
        hits = []
        for p in _iter_prod_py():
            code = _strip_comments(p.read_text(encoding="utf-8", errors="replace"))
            if re.search(r"\.\s*enforce_retention_policy\s*\(", code):
                hits.append(str(p.relative_to(BACKEND_ROOT)))
        assert hits, (
            "生产代码里**没有任何** enforce_retention_policy 的调用点 —— "
            "保留策略又变成「有实现、有测试、没人调」了"
        )

    def test_archive_retention_is_called_from_production(self):
        hits = []
        for p in _iter_prod_py():
            code = _strip_comments(p.read_text(encoding="utf-8", errors="replace"))
            if re.search(r"\.\s*enforce_archive_retention\s*\(", code):
                hits.append(str(p.relative_to(BACKEND_ROOT)))
        assert hits, "生产代码里没有 enforce_archive_retention 的调用点"

    def test_scheduler_started_in_run_py(self):
        """`run.py` 必须**真的调用**调度器 —— 只 `import` 不算接线。

        ⚠️ 判据必须落在**调用点**上：第一版写的是
        `assert "start_retention_scheduler" in code`，而 `run.py` 里
        `from core.retention_scheduler import start_retention_scheduler` 这一行
        **本身就满足它** → 变异「把调用删掉、只留 import」**没被抓住**
        （变异台实测）。这属于 skill `ci-gate-hardening` 守卫 2 的经典形态：
        断言「符号出现过」而不是「被调用了」。
        """
        code = _strip_comments((BACKEND_ROOT / "run.py").read_text(encoding="utf-8"))
        calls = [
            ln for ln in code.splitlines()
            if re.search(r"start_retention_scheduler\s*\(", ln) and "import" not in ln
        ]
        assert calls, (
            "run.py 没有**调用** start_retention_scheduler"
            "（只有 import 不算接线）—— 加了模块但没接上，等于没做"
        )

    def test_config_has_retention_with_explicit_enable(self):
        import yaml
        cfg = yaml.safe_load(
            (BACKEND_ROOT / "配置" / "system.yaml").read_text(encoding="utf-8")) or {}
        ret = (cfg.get("database") or {}).get("retention")
        assert isinstance(ret, dict), "配置/system.yaml 里没有 database.retention"
        # 删数据不可逆 → 必须显式开关，且必须是真（否则线上永远不会跑）
        assert ret.get("enabled") is True, (
            "database.retention.enabled 不是 true —— 调度器会 fail-safe 什么都不做"
        )
        for key in ("raw_data_days", "alarm_days", "compressed_data_days"):
            assert isinstance(ret.get(key), int) and ret[key] > 0, (
                f"database.retention.{key} 缺失或非法：{ret.get(key)!r}"
            )

    def test_manual_cleanup_endpoint_reads_the_same_source(self):
        """手动清理端点的默认值必须取自配置，不能再写死一个数。"""
        code = _strip_comments(
            (BACKEND_ROOT / "展示层" / "api" / "api_ops.py").read_text(encoding="utf-8"))
        # 找到 cleanup_history 函数体
        m = re.search(r"def cleanup_history\(\):(.*?)(?=\n@|\ndef )", code, re.S)
        assert m, "找不到 cleanup_history"
        body = m.group(1)
        assert "read_retention_config" in body, (
            "cleanup_history 没有从配置读默认保留天数 —— "
            "会和 database.retention.raw_data_days 形成两个口径"
        )
        assert "retention_days', 90" not in body, (
            "cleanup_history 又把 90 写死了 —— 配置里是 30，同一个系统两个口径"
        )


# ---------------------------------------------------------------------------
# 2. read_retention_config：fail-safe 口径
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "system.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


class TestReadRetentionConfig:
    def test_reads_all_windows(self, tmp_path):
        from core.retention_scheduler import read_retention_config
        p = _write_cfg(tmp_path, """
            database:
              retention:
                enabled: true
                raw_data_days: 7
                alarm_days: 14
                compressed_data_days: 365
        """)
        cfg = read_retention_config(str(p))
        assert cfg == {"history_days": 7, "alarm_days": 14, "archive_days": 365}

    def test_missing_section_returns_none(self, tmp_path):
        """配置段缺失 → None（调用方据此什么都不删）。"""
        from core.retention_scheduler import read_retention_config
        p = _write_cfg(tmp_path, "database:\n  type: sqlite\n")
        assert read_retention_config(str(p)) is None

    def test_enabled_absent_means_disabled(self, tmp_path):
        """没写 enabled → 当作**未启用**（删数据必须显式开）。"""
        from core.retention_scheduler import read_retention_config
        p = _write_cfg(tmp_path, """
            database:
              retention:
                raw_data_days: 7
                alarm_days: 14
                compressed_data_days: 365
        """)
        assert read_retention_config(str(p)) is None

    def test_enabled_false_means_disabled(self, tmp_path):
        from core.retention_scheduler import read_retention_config
        p = _write_cfg(tmp_path, """
            database:
              retention:
                enabled: false
                raw_data_days: 7
        """)
        assert read_retention_config(str(p)) is None

    @pytest.mark.parametrize("bad", ["0", "-5", "abc", "null"])
    def test_illegal_window_falls_back_not_to_zero(self, tmp_path, bad):
        """非法/非正数窗口 → **回退默认**，绝不按 0 处理（0 = 删光）。"""
        from core.retention_scheduler import read_retention_config, WINDOW_DEFAULTS
        p = _write_cfg(tmp_path, f"""
            database:
              retention:
                enabled: true
                raw_data_days: {bad}
                alarm_days: 14
                compressed_data_days: 365
        """)
        cfg = read_retention_config(str(p))
        assert cfg is not None
        assert cfg["history_days"] == WINDOW_DEFAULTS["history_days"]
        assert cfg["history_days"] > 0

    def test_unreadable_path_returns_none(self, tmp_path):
        from core.retention_scheduler import read_retention_config
        assert read_retention_config(str(tmp_path / "nope.yaml")) is None


# ---------------------------------------------------------------------------
# 3. enforce_once：真库上的行为
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    from 存储层.database import Database
    d = Database(str(tmp_path / "retention.db"))
    yield d
    d.close()


def _count(database, table: str) -> int:
    with database.get_connection(readonly=True) as conn:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


class TestEnforceOnce:
    def test_deletes_only_outside_the_window(self, db):
        from core.retention_scheduler import enforce_once
        now = datetime.now()
        db.insert_data('dev1', 'temp', 25.0, now - timedelta(days=40), 'C')   # 超 30 天 → 删
        db.insert_data('dev1', 'temp', 26.0, now - timedelta(days=1), 'C')    # 窗口内 → 留
        db.insert_alarm('a1', 'dev1', 'temp', 'warning', 'old', 80.0, 85.0,
                        now - timedelta(days=200))                            # 超 90 天 → 删
        db.insert_alarm('a2', 'dev1', 'temp', 'warning', 'new', 80.0, 85.0,
                        now - timedelta(days=3))                              # 窗口内 → 留

        result = enforce_once(db, {"history_days": 30, "alarm_days": 90, "archive_days": 365})

        assert result.get("history_deleted") == 1
        assert result.get("alarm_deleted") == 1
        assert _count(db, "history_data") == 1
        assert _count(db, "alarm_records") == 1

    def test_never_touches_realtime_data(self, db):
        """`realtime_data` 是「最新值缓存」，按时间删会让看板整片空白。"""
        from core.retention_scheduler import enforce_once
        now = datetime.now()
        db.insert_data('dev1', 'temp', 25.0, now - timedelta(days=400), 'C')
        enforce_once(db, {"history_days": 1, "alarm_days": 1, "archive_days": 1})
        assert _count(db, "realtime_data") == 1, (
            "realtime_data 被保留策略清空了 —— 设备列表当前值会集体消失"
        )

    def test_archive_retention_uses_archive_date(self, db):
        from core.retention_scheduler import enforce_once
        old_day = (datetime.now() - timedelta(days=400)).date().isoformat()
        new_day = (datetime.now() - timedelta(days=2)).date().isoformat()
        with db.get_connection() as conn:
            for day in (old_day, new_day):
                conn.execute(
                    "INSERT INTO history_archive (device_id, register_name, avg_value,"
                    " min_value, max_value, sample_count, archive_date)"
                    " VALUES ('dev1','temp',25.0,24.0,26.0,10,?)", (day,))
        enforce_once(db, {"history_days": 30, "alarm_days": 90, "archive_days": 365})
        assert _count(db, "history_archive") == 1

    def test_disabled_config_deletes_nothing(self, db):
        """cfg=None 且真实配置被禁用 → 返回空字典，且**一行都没删**。"""
        from core import retention_scheduler as rs
        now = datetime.now()
        db.insert_alarm('a1', 'dev1', 'temp', 'warning', 'old', 80.0, 85.0,
                        now - timedelta(days=999))
        before = _count(db, "alarm_records")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(rs, "read_retention_config", lambda *a, **k: None)
            assert rs.enforce_once(db) == {}
        assert _count(db, "alarm_records") == before, (
            "配置不可用时仍然删了数据 —— 删数据不可逆，必须 fail-safe"
        )

    def test_empty_cfg_deletes_nothing(self, db):
        from core.retention_scheduler import enforce_once
        now = datetime.now()
        db.insert_alarm('a1', 'dev1', 'temp', 'warning', 'old', 80.0, 85.0,
                        now - timedelta(days=999))
        assert enforce_once(db, {}) == {}
        assert _count(db, "alarm_records") == 1


# ---------------------------------------------------------------------------
# 4. 真跑：调度线程确实会执行
# ---------------------------------------------------------------------------

class TestSchedulerReallyRuns:
    def test_scheduler_thread_actually_enforces(self, db):
        """起真线程 + 零延迟 → 过期数据必须真的被删掉。

        ⚠️ 这是**正向对照**：没有它，上面所有「不该删的没删」用例
        都可能因为「调度器根本没跑」而假绿。
        """
        from core.retention_scheduler import start_retention_scheduler
        now = datetime.now()
        db.insert_alarm('a1', 'dev1', 'temp', 'warning', 'ancient', 80.0, 85.0,
                        now - timedelta(days=999))
        assert _count(db, "alarm_records") == 1

        t = start_retention_scheduler(db, interval_hours=1, first_delay_sec=0)
        try:
            assert t is not None and t.daemon, "调度线程必须是 daemon（不能阻塞退出）"

            deadline = time.time() + 10
            while time.time() < deadline and _count(db, "alarm_records") > 0:
                time.sleep(0.2)

            assert _count(db, "alarm_records") == 0, (
                "调度线程跑了 10 秒都没删掉 999 天前的报警 —— 接线没生效"
            )
        finally:
            # **必须收尾**：留下活线程会让「线程不是 daemon」的变异
            # 把 pytest 挂死（600s 超时）而不是干脆报红 —— round 209 实测踩到。
            t.stop_event.set()
            t.join(timeout=5)

    def test_scheduler_is_daemon_and_named(self, db):
        from core.retention_scheduler import start_retention_scheduler
        t = start_retention_scheduler(db, interval_hours=6, first_delay_sec=30)
        try:
            assert t.daemon is True, (
                "调度线程不是 daemon —— 进程退出时会被它拖住"
            )
            assert t.name == "retention_scheduler"
        finally:
            t.stop_event.set()
            t.join(timeout=5)

    def test_stop_event_actually_stops_the_loop(self, db):
        """`stop_event` 必须真的能让循环退出（否则测试只能靠 daemon 语义收尾）。"""
        from core.retention_scheduler import start_retention_scheduler
        t = start_retention_scheduler(db, interval_hours=6, first_delay_sec=300)
        t.stop_event.set()
        t.join(timeout=5)
        assert not t.is_alive(), "置位 stop_event 之后调度线程还在跑"
