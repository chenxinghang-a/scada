# -*- coding: utf-8 -*-
"""并发元测试：运行期会被修改的 `self.<dict>` 不得在锁外直接迭代。

为什么需要（round 373）
------------------------
审计里"并发改字典"这一类出现了至少 5 次（TSDB 注册表、边缘决策规则、
健康评分、MQTT 订阅表、仿真覆盖层），症状统一是：

    RuntimeError: dictionary changed size during iteration

它不会崩进程，只会让**那一轮**逻辑整段跳过：联锁不执行、能源结果丢失、
订阅恢复一半。日志里往往只有一条被 `except` 吞掉的 ERROR。

判据
----
只抓两个条件同时成立的循环：

1. `for ... in self.<attr>.items():` —— 直接迭代活字典（最危险形态）；
2. `<attr>` 在 `__init__` 之外被**整体修改**过（下标赋值 / del / pop /
   clear / update / setdefault / add / discard / remove）。

初始化后再也不变的配置表（`devices_config`、`node_configs`、类常量）不会被抓；
`items()` 之外的形式（`for x in self.list_attr`）也不会 —— 列表迭代不抛
RuntimeError，误报会稀释这条守卫的价值。

豁免必须写理由，并且键必须仍然命中（防止改名后豁免变成僵尸）。
"""
from __future__ import annotations

import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    "build", "dist", "legacy", "tests", "测试", "tools",
}

#: 运行期会改变容器内容的操作（不含 append/extend：列表迭代不抛 RuntimeError）
MUTATORS = {"pop", "popitem", "clear", "update", "setdefault",
            "discard", "add", "remove"}

#: 允许的锁外迭代：(相对路径, 类名, 属性名) -> 理由
ALLOWED_UNLOCKED_ITERATION: dict[tuple[str, str, str], str] = {
    (
        "采集层/device_behavior_simulator.py",
        "DeviceBehaviorSimulator",
        "_point_overrides",
    ): "仿真器本身不持锁：所有写入（hub.write_point）与读取（hub._tick_once）"
       "都在 `DeviceDataHub._lock` 内串行（round 373 已把 write_point 的 setter 收进锁）。",
    (
        "采集层/device_behavior_simulator.py",
        "DeviceBehaviorSimulator",
        "active_faults",
    ): "同上：故障注入/复位走 hub 的写寄存器路径，与 tick 同锁。",
    (
        "模拟服务端/opcua_server.py",
        "OpcuaSimServer",
        "_nodes",
    ): "只在 `start()` 建树阶段写入，推送循环在节点表构建完成后才启动。",
    (
        "timeseries/query_builder.py",
        "QueryBuilder",
        "_aggregate_funcs",
    ): "builder 是每次查询新建的一次性对象，不跨线程共享。",
}


def _iter_py():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _self_attr(node: ast.AST) -> str | None:
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id == "self":
        return node.attr
    return None


def _is_lock_with(node: ast.AST) -> bool:
    if not isinstance(node, ast.With):
        return False
    for item in node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Attribute) and "lock" in expr.attr.lower():
            return True
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
                and "lock" in expr.func.attr.lower():
            return True
    return False


def _mutated_attrs(cls: ast.ClassDef) -> set[str]:
    """`__init__` 之外被整体修改过的属性。"""
    attrs: set[str] = set()
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name == "__init__":
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript):
                        owner = _self_attr(target)
                        if owner:
                            attrs.add(owner)
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    owner = _self_attr(target)
                    if owner:
                        attrs.add(owner)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in MUTATORS:
                    owner = _self_attr(node.func.value)
                    if owner:
                        attrs.add(owner)
    return attrs


def _collect_unlocked_iterations():
    """返回 (违规列表, 命中到的豁免键集合)。"""
    violations: list[str] = []
    used_allowlist: set[tuple[str, str, str]] = set()

    for path in _iter_py():
        rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (OSError, SyntaxError):
            continue

        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            mutable = _mutated_attrs(cls)
            if not mutable:
                continue

            def walk(node: ast.AST, in_lock: bool):
                for child in ast.iter_child_nodes(node):
                    next_in_lock = in_lock or _is_lock_with(child)
                    if (not in_lock
                            and isinstance(child, (ast.For, ast.AsyncFor))
                            and isinstance(child.iter, ast.Call)
                            and isinstance(child.iter.func, ast.Attribute)
                            and child.iter.func.attr == "items"):
                        attr = _self_attr(child.iter.func.value)
                        if attr in mutable:
                            key = (rel, cls.name, attr)
                            if key in ALLOWED_UNLOCKED_ITERATION:
                                used_allowlist.add(key)
                            else:
                                violations.append(f"{rel}:{child.lineno} "
                                                  f"{cls.name}.{attr}")
                    walk(child, next_in_lock)

            walk(cls, False)

    return violations, used_allowlist


def test_no_unlocked_iteration_of_mutable_containers():
    violations, _ = _collect_unlocked_iterations()
    assert not violations, (
        "以下循环在锁外直接迭代**运行期会被修改**的字典 —— 并发时会抛 "
        "`RuntimeError: dictionary changed size during iteration`，"
        "导致整轮逻辑被跳过（联锁不执行/结果丢失）：\n  "
        + "\n  ".join(sorted(violations)) +
        "\n修法：`with self._lock: items = list(self._x.items())` 后再遍历；"
        "确属初始化后不变的请加进 ALLOWED_UNLOCKED_ITERATION 并写明理由。"
    )


def test_allowlist_entries_still_match():
    """豁免必须仍然命中 —— 否则改名/重构后它变成永久免死金牌。"""
    _, used = _collect_unlocked_iterations()
    stale = sorted(set(ALLOWED_UNLOCKED_ITERATION) - used)
    assert not stale, f"以下豁免已不再命中，请删除：{stale}"
