# -*- coding: utf-8 -*-
"""「派生指标被 `**spread` 覆盖」守卫 + 采集器队列深度行为验证。

缺陷类别：**写了被覆盖 —— 不报错，只是值永远是旧的**
------------------------------------------------------
Python 的 dict 字面量里**后出现的键覆盖前面的**。所以：

    return {
        'queue_size': self.data_queue.qsize(),   # ← 实时算出来的
        **self.stats,                            # ← 里面也有 'queue_size'
    }

`self.stats` 里的那个会赢 —— 实时值被**静默丢掉**。

实测（2026-10-08，round 194）：`采集层/data_collector.py::get_stats()`
就是这种写法，而 `self.stats` 初始化带 `'queue_size': 0`：

    >>> 队列里放 5 条
    >>> data_queue.qsize()             → 5
    >>> get_stats()['queue_size']      → 0     ← 恒为陈旧值

**队列深度是监控上的「积压」指标** —— 永远报 0 意味着积压看不出来。

为什么需要**静态**守卫（行为测试不够）
--------------------------------------
行为测试只能盖住「已知有问题的那个函数」。这类写法可以在任何类里复现，
所以配一个静态扫描（`tools/scan_shadowed_dict_keys.py`）覆盖全仓。

⚠️ 那个扫描器**第一版是瞎的**，本文件因此也把它自己的边界行为钉住：
第一版只看 `node.keys`，而 `**x` 展开在 **`node.values`** 里
（`keys` 对应位是 `None`）；且只认 `Assign`，不认
`self.x: dict = {...}` 这种 **`AnnAssign`**。两个错误叠起来 →
它对**已知有缺陷的代码**也报「✅ 干净」。
**是拿修复前的代码回放才发现工具没牙齿的**（不是靠读代码猜）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

_SCANNER_PATH = BACKEND_ROOT / "tools" / "scan_shadowed_dict_keys.py"


@pytest.fixture(scope="module")
def scanner():
    """以模块方式加载 tools/scan_shadowed_dict_keys.py（tools 不是包）。"""
    spec = importlib.util.spec_from_file_location("_scan_shadowed", _SCANNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scan_snippet(scanner, tmp_path: Path, source: str) -> list[dict]:
    """把源码写进临时文件再扫，返回 findings。"""
    f = tmp_path / "snippet.py"
    f.write_text(source, encoding="utf-8")
    # scan_file 用相对路径展示，需要文件在仓库内 —— 这里直接调内部逻辑，
    # 用一个假的 relative_to 包一层（tmp_path 不在仓库里）
    original = scanner.REPO
    scanner.REPO = tmp_path
    try:
        findings, _skipped = scanner.scan_file(f)
    finally:
        scanner.REPO = original
    return findings


# ---------------------------------------------------------------------------
# 扫描器自身的边界行为（守卫自己也要验）
# ---------------------------------------------------------------------------

class TestScannerSelfTests:
    """用合成源码证明扫描器**有牙齿**，而不是只会说「干净」。"""

    def test_detects_shadowed_key(self, scanner, tmp_path):
        findings = _scan_snippet(scanner, tmp_path, (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.stats = {'queue_size': 0}\n"
            "    def get_stats(self):\n"
            "        return {'queue_size': 1, **self.stats}\n"
        ))
        assert len(findings) == 1
        assert findings[0]["key"] == "queue_size"
        assert findings[0]["spread"] == "self.stats"

    def test_detects_shadowed_key_with_annassign_init(self, scanner, tmp_path):
        """`self.stats: dict = {...}` 是 AnnAssign —— 第一版就是漏在这里。"""
        findings = _scan_snippet(scanner, tmp_path, (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.stats: dict = {'k': 0}\n"
            "    def f(self):\n"
            "        return {'k': 1, **self.stats}\n"
        ))
        assert len(findings) == 1, "带类型注解的初始化没被认出来"

    def test_detects_key_added_by_later_subscript_write(self, scanner, tmp_path):
        """初始化里没有、但后面用 `self.stats['k'] = …` 写进去的键，同样会被覆盖。

        这一条很关键：**光删初始化里的键、留着后面的写入，缺陷会在运行时复活**。
        """
        findings = _scan_snippet(scanner, tmp_path, (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.stats = {}\n"
            "    def bump(self):\n"
            "        self.stats['queue_size'] = 0\n"
            "    def f(self):\n"
            "        return {'queue_size': 1, **self.stats}\n"
        ))
        assert len(findings) == 1

    def test_correct_order_is_not_reported(self, scanner, tmp_path):
        """**正向对照**：展开放在前面时，后面的显式键覆盖它 —— 这是**正确**写法。"""
        findings = _scan_snippet(scanner, tmp_path, (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.stats = {'queue_size': 0}\n"
            "    def f(self):\n"
            "        return {**self.stats, 'queue_size': 1}\n"
        ))
        assert findings == []

    def test_unrelated_keys_are_not_reported(self, scanner, tmp_path):
        findings = _scan_snippet(scanner, tmp_path, (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.stats = {'other': 0}\n"
            "    def f(self):\n"
            "        return {'queue_size': 1, **self.stats}\n"
        ))
        assert findings == []

    def test_non_self_spread_is_skipped_not_guessed(self, scanner, tmp_path):
        """外部对象 / 局部变量的展开：**跳过并计数**，不假装扫干净了。"""
        f = tmp_path / "snippet.py"
        f.write_text(
            "def f(base):\n"
            "    return {'k': 1, **base}\n",
            encoding="utf-8",
        )
        original = scanner.REPO
        scanner.REPO = tmp_path
        try:
            findings, skipped = scanner.scan_file(f)
        finally:
            scanner.REPO = original
        assert findings == []
        assert skipped == 1, "非 self 的展开必须被计入「跳过」，否则漏报面是隐形的"


# ---------------------------------------------------------------------------
# 全仓断言
# ---------------------------------------------------------------------------

class TestRepoHasNoShadowedDerivedMetrics:
    def test_no_shadowed_keys_in_production_code(self, scanner):
        report = scanner.scan()
        assert report["files_scanned"] > 50, (
            f"只扫到 {report['files_scanned']} 个文件 —— 扫描口径可能坏了"
        )
        findings = report["findings"]
        assert findings == [], (
            "以下位置有「显式键被后面的 **spread 覆盖」—— dict 字面量里后出现的键赢，"
            "所以显式算出来的值被静默丢掉，返回的是 spread 里那份陈旧值：\n  "
            + "\n  ".join(
                f"{f['file']}:{f['line']} {f['class']} 键 {f['key']!r} "
                f"被 **{f['spread']}（第 {f['spread_line']} 行）覆盖"
                for f in findings
            )
        )


# ---------------------------------------------------------------------------
# 行为验证：队列深度必须是**实时**的
# ---------------------------------------------------------------------------

class TestCollectorQueueSizeIsLive:
    """行为级验证 —— 静态扫描之外的独立证据。"""

    @staticmethod
    def _collector():
        from unittest.mock import MagicMock

        from 采集层.data_collector import DataCollector

        return DataCollector(MagicMock(), MagicMock(), MagicMock())

    def test_queue_size_matches_live_depth(self):
        collector = self._collector()
        for i in range(5):
            collector.data_queue.put({"value": i})

        reported = collector.get_stats()["queue_size"]
        actual = collector.data_queue.qsize()
        assert reported == actual, (
            f"get_stats()['queue_size'] = {reported}，而真实队列深度 = {actual} —— "
            "指标报的是陈旧值（修复前恒为 0）"
        )

    def test_queue_size_is_zero_when_empty(self):
        collector = self._collector()
        assert collector.get_stats()["queue_size"] == 0

    def test_queue_size_tracks_changes(self):
        collector = self._collector()
        for n in (1, 3, 7):
            while collector.data_queue.qsize() < n:
                collector.data_queue.put({"value": "x"})
            assert collector.get_stats()["queue_size"] == collector.data_queue.qsize()


class TestQueueSizeIsDerivedNotStored:
    """`queue_size` 是**派生量**，不许被存进 `self.stats`。

    为什么单独钉一条：光把 `self.stats` 初始化里的键删掉是不够的 ——
    只要还有 `self.stats['queue_size'] = …` 这样的写入，缺陷会在
    **第一次批处理之后复活**，而且静态扫描要等它复活才报。
    """

    def test_no_queue_size_write_into_stats(self):
        import ast

        src = (BACKEND_ROOT / "采集层" / "data_collector.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        offenders: list[int] = []
        for node in ast.walk(tree):
            # self.stats['queue_size'] = ...
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (
                        isinstance(t, ast.Subscript)
                        and isinstance(t.value, ast.Attribute)
                        and t.value.attr == "stats"
                        and isinstance(t.value.value, ast.Name)
                        and t.value.value.id == "self"
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "queue_size"
                    ):
                        offenders.append(node.lineno)
        assert offenders == [], (
            f"data_collector.py 第 {offenders} 行仍在往 self.stats 里写 'queue_size' —— "
            "它会被 `**self.stats` 展开覆盖掉 get_stats() 实时算的值"
        )

    def test_queue_size_is_absent_from_stats_literal(self):
        import ast

        src = (BACKEND_ROOT / "采集层" / "data_collector.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(getattr(node, "value", None), ast.Dict)
            ):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "stats"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    keys = {
                        k.value for k in node.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
                    assert "queue_size" not in keys, (
                        f"data_collector.py 第 {node.lineno} 行的 self.stats 初始化里"
                        "又出现了 'queue_size' —— 它会覆盖 get_stats() 的实时值"
                    )


# ---------------------------------------------------------------------------
# 注入型信号：写了 setter，就得有人调 —— 或者**显式声明**它故意不可用
# ---------------------------------------------------------------------------

#: 已确认**当前没有被注入**的信号源，及理由。
#:
#: 为什么单列一张表：这类 setter 的失败方式同样是**静默**的 ——
#: 没人调用它，代码也不会报错，只是那一路信号恒为「不可用」，
#: 而负载评估会**安静地**退化成只用剩下的信号。
UNWIRED_SIGNAL_INJECTORS: dict[str, str] = {
    "set_queue_size_func": (
        "SystemLoadMonitor 的**队列深度**信号。当前无人调用 → `_get_queue_size()` "
        "恒返回 None → `'queue'` 一直挂在 `unavailable_signals` 里，"
        "负载等级只由 CPU + 内存决定，**限流不会因队列积压而收紧**。"
        "⚠️ 接线前必须先修 `get_stats()['queue_size']` —— 它修复前**恒为 0**，"
        "直接接上去等于注入该模块注释里明确警告的「系统很闲」假信号。"
        "该前提已随本轮完成；**接线本身属运维口径改动，见决策简报 D15**。"
    ),
}


class TestSignalInjectorsAreWiredOrDeclared:
    """注入型 setter 必须「有人调」或「显式声明为不接」。

    本守卫只能做**单向**断言（表里声明的确实没被接线）——
    「新出现一个没人调的 setter」静态上无法与「本来就不需要注入」区分开，
    所以这一半靠 code review，不假装守住了。
    """

    @staticmethod
    def _production_sources() -> list[Path]:
        dirs = ["core", "展示层", "报警层", "采集层", "存储层", "智能层",
                "用户层", "timeseries", "gateway"]
        out: list[Path] = []
        for d in dirs:
            root = BACKEND_ROOT / d
            if root.is_dir():
                out.extend(
                    p for p in root.rglob("*.py")
                    if "__pycache__" not in p.parts
                )
        for f in ("run.py", "launcher.py"):
            p = BACKEND_ROOT / f
            if p.is_file():
                out.append(p)
        return out

    def test_declared_unwired_injectors_are_really_unwired(self):
        import re

        sources = self._production_sources()
        assert len(sources) > 50, f"只扫到 {len(sources)} 个文件 —— 扫描口径可能坏了"

        still_wired: list[str] = []
        for name in UNWIRED_SIGNAL_INJECTORS:
            call_re = re.compile(rf"\.{name}\s*\(")
            def_re = re.compile(rf"^\s*def\s+{name}\s*\(")
            hits: list[str] = []
            for f in sources:
                text = f.read_text(encoding="utf-8", errors="replace")
                # 同名 def 的缩进：落在它里面的调用是**转发**（API 本体），不是接线。
                # 踩过：第一版没排除这层，把
                # `DynamicRateLimiter.set_queue_size_func` 里转发给 monitor 的那一行
                # 当成了「有人接线」→ 守卫假红。
                forward_indent: int | None = None
                for i, line in enumerate(text.splitlines(), 1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    indent = len(line) - len(line.lstrip())
                    m = def_re.match(line)
                    if m:
                        forward_indent = indent
                        continue
                    if forward_indent is not None:
                        if indent > forward_indent:
                            continue  # 还在那个同名函数体内 → 转发，不算接线
                        forward_indent = None
                    if call_re.search(line):
                        hits.append(f"{f.relative_to(BACKEND_ROOT).as_posix()}:{i}")
            if hits:
                still_wired.append(f"{name} → {hits}")

        assert still_wired == [], (
            "以下注入型 setter 已经被接线了，请从 UNWIRED_SIGNAL_INJECTORS 删掉，"
            "并同步更新决策简报 D15：\n  " + "\n  ".join(still_wired)
        )

    def test_declarations_have_reasons(self):
        thin = [k for k, v in UNWIRED_SIGNAL_INJECTORS.items() if len(v.strip()) < 40]
        assert thin == [], f"以下声明没写清理由与前提：{thin}"
