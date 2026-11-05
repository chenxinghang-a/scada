# -*- coding: utf-8 -*-
"""生命周期元测试：`forget_device()` 必须清掉**所有按设备分键的容器**。

为什么需要（round 372）
------------------------
审计里同一类缺陷出现了至少四次：删设备时清理写了一半 ——
`_alerted`（set 不适用 `pop(key, None)`，整条漏掉）、`_status_cache`、
`_device_locks`、`_nonfinite_warn_at`……共同点是：

* 容器按 `device_id`（或 `device_id:register` 复合键）分键；
* 创建路径写得很全，销毁路径靠"没人删设备就不会出事"；
* 漏一个不会报错，只会在"同名设备重建"或"报表对账"时表现为幽灵数据。

逐个记是记不住的。这里用 AST 反射：对每个定义了 `forget_device` 的类，
找出**被 device_id 索引过的属性**，再比对 `forget_device` 里实际清理了哪些。
漏清的会在断言里列出来。

豁免必须写理由（`ALLOWED_MISSING`），并且理由要能说服 reviewer。
"""
from __future__ import annotations

import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    "build", "dist", "legacy", "tests", "测试", "tools",
}

#: 清理动作：调用这些方法说明该容器被处理了
CLEAN_VERBS = {"pop", "clear", "discard", "remove", "popleft"}

#: 复合键常用的局部变量名。**不要**把泛化的 `key` 放进来 ——
#: 阈值表 `self.thresholds[key]` 的 key 是寄存器名，会被误判成设备键。
KEY_NAMES = {"device_id", "score_key", "alarm_key", "composite_key", "device_key"}

#: 允许漏清：(相对路径, 类名, 属性名) -> 理由
ALLOWED_MISSING: dict[tuple[str, str, str], str] = {
    (
        "采集层/data_collector.py",
        "DataCollector",
        "_cancelled_devices",
    ): "**故意不清**：它是「删除后短期禁止重新调度」的登记，清掉会让在途采集"
       "重新挂上定时器（僵尸采集回归）；由 `_prune_cancelled()` 按保留期回收。"
       "见 `CANCELLED_REGISTRY_ATTRS` 的注释。",
    (
        "采集层/data_collector.py",
        "DataCollector",
        "tasks",
    ): "定时器由 `remove_device_task()` 负责取消（删除路径先调它再调 forget_device），"
       "两者职责不重叠；在这里 pop 会漏掉 Timer.cancel()。",
}


def _iter_py():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _self_attr(node: ast.AST) -> str | None:
    """`self.foo` / `self.foo[...]` → 'foo'。"""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id == "self":
        return node.attr
    return None


def _mentions_device(node: ast.AST, key_vars: set[str]) -> bool:
    if node is None:
        return False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in key_vars:
            return True
        if isinstance(sub, ast.JoinedStr):
            if any(isinstance(v, ast.FormattedValue) for v in sub.values):
                if any('device' in ast.dump(v) or 'key' in ast.dump(v)
                       for v in sub.values):
                    return True
    return False


def _local_key_vars(func: ast.AST) -> set[str]:
    """函数内由 f-string / format 拼出来的键变量（`key = f"{device_id}:..."`）。"""
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.JoinedStr) or
                (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                 and node.value.func.attr == "format")):
            continue
        if "device" not in ast.dump(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _device_keyed_attrs(cls: ast.ClassDef) -> set[str]:
    """找出被 device_id（或复合键）索引过的 `self.<attr>`。"""
    attrs: set[str] = set()
    for func in [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        key_vars = KEY_NAMES | _local_key_vars(func)
        for node in ast.walk(func):
            if isinstance(node, ast.Subscript):
                owner = _self_attr(node.value)
                if owner and _mentions_device(node.slice, key_vars):
                    attrs.add(owner)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr not in ({"get", "setdefault", "pop"} | CLEAN_VERBS):
                    continue
                owner = _self_attr(node.func.value)
                if owner and node.args and _mentions_device(node.args[0], key_vars):
                    attrs.add(owner)
    return attrs


def _cleaned_attrs(method: ast.AST, methods: dict[str, ast.AST] | None = None,
                   depth: int = 0) -> set[str]:
    """`forget_device` 里实际清理掉的属性。

    `methods` 用于**跟随一层委托**：很多类把实际清理放在
    `_forget_device_state()` 里，`forget_device` 只是别名入口
    （`DataCollector` 就是这种写法）。不跟随会把已清理的属性误报成漏清。
    """
    cleaned: set[str] = set()
    for node in ast.walk(method):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in CLEAN_VERBS:
                owner = _self_attr(node.func.value)
                if owner:
                    cleaned.add(owner)
            elif (methods is not None and depth < 2
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "self"
                  and node.func.attr in methods):
                cleaned |= _cleaned_attrs(methods[node.func.attr], methods, depth + 1)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                owner = _self_attr(target)
                if owner:
                    cleaned.add(owner)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and isinstance(node.value, ast.AST):
                    if _self_attr(target) and isinstance(
                            node.value, (ast.Dict, ast.Set, ast.List, ast.Call)):
                        cleaned.add(target.attr)
        elif isinstance(node, ast.For):
            # `for container in (a, b, c): container.pop(key)`
            if not isinstance(node.target, ast.Name):
                continue
            body_calls = [
                c for stmt in node.body for c in ast.walk(stmt)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr in CLEAN_VERBS
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == node.target.id
            ]
            if body_calls:
                for elt in ast.walk(node.iter):
                    owner = _self_attr(elt)
                    if owner:
                        cleaned.add(owner)
    return cleaned


def _collect_missing() -> list[str]:
    missing: list[str] = []
    for path in _iter_py():
        rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (OSError, SyntaxError):
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            method = next(
                (n for n in cls.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "forget_device"),
                None,
            )
            if method is None:
                continue
            keyed = _device_keyed_attrs(cls)
            methods = {
                n.name: n for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            cleaned = _cleaned_attrs(method, methods)
            for attr in sorted(keyed - cleaned):
                if (rel, cls.name, attr) in ALLOWED_MISSING:
                    continue
                missing.append(f"{rel}::{cls.name}.{attr}")
    return missing


def test_forget_device_clears_every_device_keyed_container():
    missing = _collect_missing()
    assert not missing, (
        "以下按 device_id 分键的容器没有在 forget_device() 里清理 —— "
        "删除设备后会残留幽灵数据（同名设备重建时继承旧状态）：\n  "
        + "\n  ".join(missing) +
        "\n修法：在 forget_device 里 pop/clear/discard 该容器；"
        "确属全局共享的请加进 ALLOWED_MISSING 并写明理由。"
    )


def test_scan_covers_real_lifecycle_classes():
    """防止扫描器写错导致"一个类都没扫到"的假绿。"""
    found = []
    for path in _iter_py():
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (OSError, SyntaxError):
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "forget_device" for n in cls.body):
                found.append(cls.name)
    for must in ("SPCAnalyzer", "PredictiveMaintenance", "VibrationAnalyzer",
                 "TSDBAdapter", "EnergyManager", "DeviceControlSafety"):
        assert must in found, f"{must} 没被扫到，扫描逻辑可能失效"
