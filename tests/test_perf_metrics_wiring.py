"""性能监控页「三零」的接线守卫（round 212，2026-10-11）。

背景
----
wb 在隔离环境端到端实测，性能监控页修好 socket 之后仍有三个零值：

* **CPU/内存 = -1.0%** —— `展示层/api/api_performance.py` 里 `import psutil` 是
  **懒导入 + `except ImportError` → `cpu_percent = -1`**，而 venv 与
  `requirements.txt` **都没有 psutil**，spec 也没声明 →
  **漏声明的依赖不报错，只会静默降级**。
* **数据库 0.0MB / 记录 0 条** —— `_collect_database_metrics` 写死
  `data/scada.db`，而**模拟模式实际用的是 `data/scada_simulated.db`**。
  更糟的是 `data/` 下**两个库同时存在**（旧 `scada.db` 495MB、
  在用 `scada_simulated.db` 712MB）→「取第一个存在的」这种猜法会**静默指错**。

这两条的共性：**声明/实现都在，但接线断了**，而且断的方式是**静默的**
（`-1` 和 `0.0` 看起来都是「合法的数字」）。

本文件钉住的是**接线**，不是数值。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = BACKEND_ROOT / "requirements.txt"
SPEC = BACKEND_ROOT / "scada-backend.spec"
ROUTES = BACKEND_ROOT / "展示层" / "routes.py"
API_PERF = BACKEND_ROOT / "展示层" / "api" / "api_performance.py"


def _strip_comments(text: str) -> str:
    """去掉 `#` 注释，避免注释里的字面量把判据带偏。"""
    out = []
    for line in text.splitlines():
        idx = line.find("#")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


def _code_string_literals(path: Path) -> list[tuple[int, str]]:
    """返回源码里**参与逻辑**的字符串字面量（`(行号, 值)`）。

    ⚠️ 刻意排除 **docstring** —— 本文件的两条「防回退」守卫第一版就是被
    **解释性注释 / docstring** 绊倒的：注释里写着「此前这里写死 `'scada.db'`」
    是**说明历史**，不是**又写死了一遍**。
    这与 round 207「核配置别用 grep，我自己的注释里就引用了被删的那行」同源：
    **判据必须区分「代码里的字面量」与「文档里的引用」。**
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            out.append((node.lineno, node.value))
    return out


# ---------------------------------------------------------------------------
# ① psutil：懒导入的依赖必须**显式声明**
# ---------------------------------------------------------------------------


class TestPsutilIsDeclared:
    def test_lazy_import_exists(self):
        """前提：代码里确实是懒导入（这正是漏声明不容易被发现的原因）。"""
        code = API_PERF.read_text(encoding="utf-8")
        assert re.search(r"^\s+import psutil\s*$", code, re.M), (
            "api_performance 里没有 `import psutil` —— 这条守卫的前提变了，请复核"
        )

    def test_declared_in_requirements(self):
        """★ 懒导入的依赖必须在 requirements.txt 里声明。

        否则「本机装过所以能跑、别人/CI/产物里没有」→ `except ImportError`
        静默把 CPU 变成 -1 —— **没有报错，只有一个看起来合法的数字**。
        """
        text = _strip_comments(REQUIREMENTS.read_text(encoding="utf-8"))
        assert re.search(r"^psutil\b", text, re.M | re.I), (
            "requirements.txt 没有声明 psutil —— 而 api_performance 会 `import psutil`。\n"
            "  漏声明的依赖不会报错，只会让性能页的 CPU/内存**静默变成 -1.0%**。"
        )

    def test_declared_in_pyinstaller_spec(self):
        """★ 打包清单里也要有 —— 否则产物里没有 psutil，部署版照样 -1.0%。

        `import psutil` 在函数体内（懒导入），静态分析虽然通常能扫到，
        但这里刻意要求**显式声明**：产物缺依赖是「装完之后才发现」的那类问题，
        代价远高于多写一个字符串。
        """
        spec = SPEC.read_text(encoding="utf-8")
        m = re.search(r"hiddenimports=\[(.*?)\]", spec, re.S)
        assert m, "scada-backend.spec 里找不到 hiddenimports 列表"
        assert "psutil" in m.group(1), (
            "scada-backend.spec 的 hiddenimports 里没有 psutil —— "
            "打包产物会缺这个依赖，部署版性能页 CPU/内存恒为 -1.0%"
        )

    def test_psutil_is_importable_here(self):
        """正向对照：在**本环境**里 psutil 必须真的可用。

        没有这条，上面两条「声明了」可能掩盖「装了但装坏了」。
        """
        psutil = pytest.importorskip("psutil", reason="本环境没装 psutil —— 声明了也没用")
        assert psutil.cpu_count() and psutil.cpu_count() > 0


class TestCpuMetricIsNotSilentlyDegraded:
    def test_cpu_percent_is_a_real_value(self):
        """★ 正向对照：psutil 可用时，CPU 必须报真值，不能是 -1。

        `-1` 是「ImportError 兜底值」—— 它和真值一样是个合法数字，
        所以**没有守卫的话，退化到 -1 是看不见的**。
        """
        pytest.importorskip("psutil")
        from 展示层.api.api_performance import PerformanceCollector

        m = PerformanceCollector()._collect_system_metrics()
        assert m.get("cpu_percent") != -1, (
            "CPU 报了 -1（ImportError 兜底值）—— psutil 声明/安装的接线又断了"
        )
        assert m.get("cpu_percent", -1) >= 0
        assert m.get("cpu_count", 0) > 0, "cpu_count 没取到"


# ---------------------------------------------------------------------------
# ② 库路径：唯一真源 = paths 的登记，禁止写死文件名
# ---------------------------------------------------------------------------


class TestActiveDbPathRegistry:
    def test_default_is_none(self, monkeypatch):
        import paths

        monkeypatch.setattr(paths, "_ACTIVE_DB_PATH", None)
        assert paths.get_active_db_path() is None, (
            "未登记时必须返回 None —— 回退到任何默认路径都会让调用方拿到"
            "「可能不是当前库」的路径去展示"
        )

    def test_set_then_get(self, monkeypatch, tmp_path):
        import paths

        monkeypatch.setattr(paths, "_ACTIVE_DB_PATH", None)
        target = tmp_path / "some.db"
        paths.set_active_db_path(str(target))
        assert paths.get_active_db_path() == target

    def test_set_none_clears(self, monkeypatch, tmp_path):
        import paths

        paths.set_active_db_path(str(tmp_path / "x.db"))
        paths.set_active_db_path(None)
        assert paths.get_active_db_path() is None

    def test_create_app_registers_the_path(self):
        """★ 接线守卫：`create_app` 里必须有**调用点**（不是只有 import）。

        round 209 的教训：`assert "名字" in code` 会被一行 `from … import 名字`
        满足 —— **「出现」≠「被调用」**。所以这里逐行找调用形态。
        """
        code = _strip_comments(ROUTES.read_text(encoding="utf-8"))
        call_lines = [
            ln for ln in code.splitlines()
            if re.search(r"\bset_active_db_path\s*\(", ln) and "import" not in ln
        ]
        assert call_lines, (
            "展示层/routes.py 的 create_app 没有**调用** paths.set_active_db_path() ——\n"
            "  那么性能指标永远拿不到当前库路径，只能报 unknown（或更糟：去猜）。\n"
            "  注意：光是 `import paths` 不算接线。"
        )


class TestDatabaseMetricsFollowTheRegistry:
    def test_unknown_when_not_registered(self, monkeypatch):
        """★ 没登记 → 显式 `unknown`，**绝不回退到猜测路径**。"""
        import paths
        from 展示层.api.api_performance import PerformanceCollector

        monkeypatch.setattr(paths, "_ACTIVE_DB_PATH", None)
        m = PerformanceCollector()._collect_database_metrics()
        assert m.get("status") == "unknown", (
            f"未登记库路径时应当报 unknown，实际 {m.get('status')!r} —— "
            "任何「猜一个路径」都会在 data/ 下有多个库时静默指错"
        )
        assert m.get("size_mb") == 0

    def test_reports_the_registered_db_not_a_hardcoded_name(self, monkeypatch, tmp_path):
        """★ 回归（wb 实测的「数据库 0.0MB」）：必须报**登记的那个库**。

        判别性：造一个**名字不是 scada.db**、大小可辨的临时库。
        旧实现（写死 `data/scada.db`）会找不到 → 报 missing/0.0；
        新实现报登记的这个 → size_mb > 0 且 db_file 对得上。
        """
        import sqlite3

        import paths
        from 展示层.api.api_performance import PerformanceCollector

        db = tmp_path / "scada_simulated.db"       # ← 刻意用「非默认」的文件名
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
        conn.commit()
        conn.close()

        monkeypatch.setattr(paths, "_ACTIVE_DB_PATH", None)
        paths.set_active_db_path(str(db))
        try:
            m = PerformanceCollector()._collect_database_metrics()
        finally:
            paths.set_active_db_path(None)

        assert m.get("status") == "ok", (
            f"登记了库却报 {m.get('status')!r} —— 指标没跟着唯一真源走"
        )
        assert m.get("db_file") == "scada_simulated.db", (
            f"报的是 {m.get('db_file')!r} —— 说明还在用写死的文件名"
        )
        assert m.get("size_mb", 0) > 0
        assert m.get("total_records") == 500, (
            f"行数统计 = {m.get('total_records')!r}，期望 500 —— 可能连到了别的库"
        )

    def test_no_hardcoded_db_filename_in_source(self):
        """★ 守卫：源码里不得再出现写死的库文件名（**只看参与逻辑的字面量**）。

        这条是「防回退」—— 上面那条回归用例只覆盖当前实现，
        而这条挡住「有人又写死一个名字」。
        """
        bad = [
            f"L{ln}: {val!r}" for ln, val in _code_string_literals(API_PERF)
            if re.search(r"scada[a-z_]*\.db", val)
        ]
        assert not bad, (
            "api_performance 里又出现了写死的库文件名 —— 必须走 paths 的登记：\n  "
            + "\n  ".join(bad)
        )

    def test_wal_path_is_derived_from_the_db_path(self):
        """WAL 是「库文件同目录同名 + `-wal`」，不能写死 `scada.db-wal`。"""
        bad = [
            f"L{ln}: {val!r}" for ln, val in _code_string_literals(API_PERF)
            if "scada.db-wal" in val
        ]
        assert not bad, (
            "还写死着 'scada.db-wal' —— 换库之后 WAL 大小就永远取不到了：\n  "
            + "\n  ".join(bad)
        )
