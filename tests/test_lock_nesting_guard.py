# -*- coding: utf-8 -*-
"""静态闸门：不允许「持锁时调用会再取同一把锁的方法」（round 230）。

为什么需要
----------
`approve_bypass()` 曾经在 `with self._bypass_lock:` 里调用
`_activate_bypass()`，而后者第一件事又是 `with self._bypass_lock:`。
`_bypass_lock` 是 `threading.Lock()`（**不可重入**）→ 死锁，
而且卡死点正好是「第二个人审批、旁路即将生效」的那一刻：
审批线程永久占着锁，之后所有旁路操作全部挂住，只有重启才能恢复。

这种 bug 的特征是**跨函数**的：
单看 `approve_bypass` 很合理（持锁改状态）；
单看 `_activate_bypass` 也很合理（保护自己的临界区）；
**放在一起**才死锁。所以读代码几乎看不出来，必须有静态检查。

判据
----
1. 从模块里的赋值收集锁类型：`self.x = threading.Lock()` 不可重入、
   `self.x = threading.RLock()` 可重入；
2. 对每个函数，找出 `with self.<lock>:` 块里对 `self.<method>()` 的调用；
3. 若被调方法**直接**也 `with self.<同一个 lock>:`，且该锁不可重入 → 违规。

只做**一层**跨函数分析：这已覆盖本类问题，且不会因为调用链长而误报一堆。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PROD_DIRS = ('采集层', '存储层', '展示层', '报警层', '智能层', '用户层', 'core')

#: 已知且**有意**保留的例外：{(相对路径, 函数名, 锁名): 原因}
ALLOWED_NESTED_LOCKS: dict[tuple[str, str, str], str] = {}


def _iter_prod_files():
    for d in PROD_DIRS:
        root = REPO / d
        if not root.exists():
            continue
        for p in root.rglob('*.py'):
            if '__pycache__' in p.parts:
                continue
            yield p


def _lock_kinds(tree: ast.Module) -> dict[str, str]:
    """`self.x = threading.Lock()` → {'x': 'Lock'}；RLock 同理。"""
    kinds: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == 'self'):
                continue
            call = node.value
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if call.func.attr in ('Lock', 'RLock'):
                kinds[tgt.attr] = call.func.attr
    return kinds


def _methods(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    out: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _self_lock_of(withitem: ast.withitem) -> str | None:
    """`with self._lock:` → '_lock'。"""
    ctx = withitem.context_expr
    if isinstance(ctx, ast.Attribute) and isinstance(ctx.value, ast.Name) and ctx.value.id == 'self':
        return ctx.attr
    return None


def _locks_held_directly(fn: ast.AST) -> set[str]:
    held = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.With):
            for item in node.items:
                name = _self_lock_of(item)
                if name:
                    held.add(name)
    return held


def _lock_then_calls(fn: ast.AST) -> list[tuple[str, str, int]]:
    """返回 [(锁名, 被调方法名, 行号)]：在 `with self.<lock>:` 块内调用了 self.<method>()。"""
    out: list[tuple[str, str, int]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.With):
            continue
        locks = [n for n in (_self_lock_of(i) for i in node.items) if n]
        if not locks:
            continue
        for inner in ast.walk(node):
            if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and isinstance(inner.func.value, ast.Name) and inner.func.value.id == 'self'):
                continue
            for lock in locks:
                out.append((lock, inner.func.attr, getattr(inner, 'lineno', 0)))
    return out


def scan_source(src: str, rel: str = '<mem>') -> list[str]:
    """扫一段源码，返回违规描述列表（供真实扫描与元测试共用）。"""
    tree = ast.parse(src)
    kinds = _lock_kinds(tree)
    methods = _methods(tree)
    hits: list[str] = []
    for name, fn in methods.items():
        for lock, callee, lineno in _lock_then_calls(fn):
            if kinds.get(lock) != 'Lock':      # RLock / 未知 → 不算
                continue
            target = methods.get(callee)
            if target is None:
                continue
            if lock in _locks_held_directly(target):
                if (rel, name, lock) in ALLOWED_NESTED_LOCKS:
                    continue
                hits.append(
                    f'{rel}:{lineno} {name}() 持 {lock} 调用 {callee}()，'
                    f'而 {callee}() 也取 {lock}（不可重入 → 死锁）'
                )
    return hits


def test_no_nested_acquisition_of_non_reentrant_lock():
    hits: list[str] = []
    for path in _iter_prod_files():
        rel = path.relative_to(REPO).as_posix()
        try:
            src = path.read_text(encoding='utf-8', errors='replace')
            hits.extend(scan_source(src, rel))
        except SyntaxError:
            continue

    assert not hits, (
        '以下位置会**死锁**：持着不可重入的 threading.Lock 调用了同样会取这把锁的方法。\n'
        '把嵌套调用移到 `with` 块外面（状态改完再激活），或把锁换成 RLock 并说明理由。\n\n'
        + '\n'.join('  ' + h for h in hits)
    )


def test_scanner_catches_the_real_bug_shape():
    """元守卫：扫描器必须能认出 round 230 那个真实形状，否则上面那条永远绿。"""
    bad = '''
import threading

class Demo:
    def __init__(self):
        self._lock = threading.Lock()

    def approve(self):
        with self._lock:
            self._activate()

    def _activate(self):
        with self._lock:
            pass
'''
    hits = scan_source(bad, 'demo.py')
    assert hits, '扫描器认不出「持锁调用会再取同一把锁的方法」'
    assert '_activate' in hits[0]


def test_scanner_allows_rlock():
    """RLock 可重入，同样的写法是合法的 —— 不能误报。"""
    ok = '''
import threading

class Demo:
    def __init__(self):
        self._lock = threading.RLock()

    def approve(self):
        with self._lock:
            self._activate()

    def _activate(self):
        with self._lock:
            pass
'''
    assert scan_source(ok, 'demo.py') == []


def test_scanner_allows_call_outside_lock():
    """修好后的形状（锁外调用）不能报。"""
    ok = '''
import threading

class Demo:
    def __init__(self):
        self._lock = threading.Lock()

    def approve(self):
        go = False
        with self._lock:
            go = True
        if go:
            self._activate()

    def _activate(self):
        with self._lock:
            pass
'''
    assert scan_source(ok, 'demo.py') == []
