# -*- coding: utf-8 -*-
"""`start_maintenance` 必须在设备连接**之前**且**独立 try**（round 216）。

背景
====
`run.py::_background_start()` 里，`start_maintenance`（含 `data_archive` 归档任务）
原先位于 try 块的**链尾**，前面依次是 `connect_all(timeout=20)` →
`start_reconnect_loop` → `_apply_ha_role` → 智能层 5 个 `start()` →
TDengine 桥接/适配器。**任一环节抛异常**，外层 `except` 捕获后
**后面全部不执行** → `start_maintenance` 不启动 → 数据归档不跑。

后果：round 209 的「数据保留策略自动接线」这个结论在**现场最常见的故障**
（设备连不上）下会失效 —— 保留策略调度器（`run.py:151`，主路径）不受影响，
**只有归档那一半**被拖累。

判据设计（为什么是这两条）
========================
1. **顺序**：`start_maintenance` 必须在 `connect_all` **之前** ——
   它是纯本地任务（DB 归档 / WAL checkpoint / 缓存清理），**逻辑上不依赖设备连接**，
   没有理由排在设备连接后面。
2. **独立 try**：它必须处在**自己的** try 块里 —— 这是「双向隔离」：
   它的失败不拖累后面的 WebSocket 注入等；设备连接的失败也不拖累它。

⚠️ 两条都用 **AST** 而不是字符串匹配 —— 本仓因「文本匹配把注释当代码」
栽过三次（注释里提到 `start_maintenance` 会让字符串判据假绿）。

元守卫：本文件自己也做**前置对照**（见 `TestGuardHasTeeth`）——
用一个合成的假 run.py 证明判据真的能区分正反例，否则「扫到 0 个」会被当成绿。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

#: 支持环境变量覆盖 —— 便于本测试在**仓库外**（草稿态）先验证判据有判别力，
#: 落进 tests/ 后走默认值（`tests/` 的上一级即仓库根）。
import os as _os
BACKEND_ROOT = pathlib.Path(
    _os.environ.get('SCADA_BACKEND_ROOT') or (pathlib.Path(__file__).resolve().parent.parent))
RUN_PY = BACKEND_ROOT / 'run.py'


def _background_start_node(tree: ast.AST) -> ast.FunctionDef:
    """取 `run.py` 里 `_background_start` 函数定义节点（缺失即判据失效，直接失败）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_background_start':
            return node
    pytest.fail(
        "run.py 里找不到 `_background_start` —— 本判据的定位失效，"
        "请更新测试（不要静默跳过，那会让这条守卫变成空转）")


def _call_line(fn: ast.FunctionDef, func_name: str) -> int | None:
    """函数体内**首次**调用 `func_name(...)` 的行号（AST 级，注释不算）。"""
    lines = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Name) and n.func.id == func_name)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == func_name)
        )
    ]
    return min(lines) if lines else None


def _innermost_try_body(fn: ast.FunctionDef, call_name: str) -> list[ast.stmt] | None:
    """包含 `call_name` 调用的**最内层** Try 的 body（没有则 None）。"""
    best: tuple[int, list[ast.stmt]] | None = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        has_call = any(
            isinstance(c, ast.Call) and (
                (isinstance(c.func, ast.Name) and c.func.id == call_name)
                or (isinstance(c.func, ast.Attribute) and c.func.attr == call_name))
            for c in ast.walk(node)
        )
        if not has_call:
            continue
        depth = sum(1 for _ in ast.walk(node))       # 越内层节点越少
        if best is None or depth < best[0]:
            best = (depth, node.body)
    return best[1] if best else None


class TestOrder:
    """① 顺序：maintenance 必须在设备连接之前。"""

    def test_maintenance_called_before_device_connect(self):
        tree = ast.parse(RUN_PY.read_text(encoding='utf-8'))
        fn = _background_start_node(tree)

        maint = _call_line(fn, 'start_maintenance')
        connect = _call_line(fn, 'connect_all')
        assert maint is not None, (
            "run.py 的 _background_start 里没有调用 start_maintenance —— "
            "接线可能被删了（这是更严重的问题，先查这个）")
        assert connect is not None, (
            "run.py 的 _background_start 里没有 connect_all —— 定位失效，更新测试")
        assert maint < connect, (
            f"start_maintenance 在第 {maint} 行，connect_all 在第 {connect} 行 —— "
            "维护任务排在设备连接**之后**，会被「设备连不上」这类异常拖累而不启动，"
            "数据归档也就不会跑。请把 start_maintenance 提到 connect_all 之前。")


class TestIsolation:
    """② 独立 try：maintenance 的 try 块里不得含设备连接（双向隔离）。"""

    def test_maintenance_has_its_own_try(self):
        tree = ast.parse(RUN_PY.read_text(encoding='utf-8'))
        fn = _background_start_node(tree)

        body = _innermost_try_body(fn, 'start_maintenance')
        assert body is not None, (
            "start_maintenance 没有包在 try 里 —— 它一旦抛异常会打断整个后台启动链")

        # 该 try 里不该出现设备连接/智能层启动（那说明它与它们共用一个 try）
        forbidden = {'connect_all', 'start_reconnect_loop', 'start'}
        leaked = sorted(
            {c.func.id for s in body for c in ast.walk(s)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
             and c.func.id in forbidden}
            | {c.func.attr for s in body for c in ast.walk(s)
               if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
               and c.func.attr in forbidden}
        )
        assert not leaked, (
            f"start_maintenance 的 try 块里还有 {leaked} —— 它与设备连接/智能层共用 try，"
            "异常会互相拖累。应给它**独立的** try（提前到 connect_all 之前）。")


class TestGuardHasTeeth:
    """元守卫：证明上面两条判据真能区分正反例（防「扫到 0 个 = 假绿」）。"""

    BAD = '''\
def _background_start():
    try:
        device_manager.connect_all(timeout=20)
        predictive_maintenance.start()
        from core.maintenance import start_maintenance
        start_maintenance(database=database)
    except Exception as e:
        logger.error(e)
'''

    GOOD = '''\
def _background_start():
    try:
        from core.maintenance import start_maintenance
        start_maintenance(database=database)
    except Exception as e:
        logger.error(e)
    try:
        device_manager.connect_all(timeout=20)
        predictive_maintenance.start()
    except Exception as e:
        logger.error(e)
'''

    def test_flags_the_bad_layout(self):
        fn = _background_start_node(ast.parse(self.BAD))
        assert _call_line(fn, 'start_maintenance') > _call_line(fn, 'connect_all'), (
            "夹具失效：坏布局里 maintenance 竟然排在 connect 之前")
        body = _innermost_try_body(fn, 'start_maintenance')
        assert any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == 'connect_all'
            for s in body for c in ast.walk(s)
        ), "夹具失效：坏布局的 try 里应当能看到 connect_all"

    def test_passes_the_good_layout(self):
        fn = _background_start_node(ast.parse(self.GOOD))
        assert _call_line(fn, 'start_maintenance') < _call_line(fn, 'connect_all')
        body = _innermost_try_body(fn, 'start_maintenance')
        assert not any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == 'connect_all'
            for s in body for c in ast.walk(s)
        ), "夹具失效：好布局的 try 里不该有 connect_all"
