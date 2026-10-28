# -*- coding: utf-8 -*-
"""扫描「没有消费方的公开方法」—— round 307。

判据：一个方法在全仓里**没有任何 `.name(` 形式的调用点**（生产代码与测试分开统计）。
这类方法不产生症状，但会在被接上的第一天制造假成功
（`set_deadband` 在 round 305 之前就是 0/0，而且只改内存不落盘）。

排除：魔术方法、property、Flask 路由处理函数（由框架调用）、pytest fixture、
显式标注 `# noqa: dead-api` 的方法。

用法：.venv/Scripts/python.exe tools/_scan_dead_api.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SKIP_DIRS = {'dist', 'backup', 'backups', 'build', 'node_modules', '.venv',
             '.git', '__pycache__', '.pytest_cache', '.vitest_cache',
             '.build-backend', '.ui-shots', 'release', 'evidence', 'exports'}

ROUTE_DECORATORS = ('route', 'before_request', 'after_request', 'teardown_appcontext',
                    'errorhandler', 'websocket', 'on', 'fixture', 'command')


def _iter_py(root: Path):
    for p in root.rglob('*.py'):
        parts = set(p.parts)
        if parts & SKIP_DIRS:
            continue
        yield p


def _has_route_decorator(node: ast.FunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr in ROUTE_DECORATORS:
            return True
    return False


def _is_property(node: ast.FunctionDef) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == 'property':
            return True
    return False


def collect_methods(root: Path):
    """返回 [(类名, 方法名, 文件, 行号, 是否公开)]。"""
    out = []
    for path in _iter_py(root):
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for node in cls.body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                if node.name.startswith('__') and node.name.endswith('__'):
                    continue
                if _has_route_decorator(node) or _is_property(node):
                    continue
                seg = ast.get_source_segment(path.read_text(encoding='utf-8'), node) or ''
                if 'noqa: dead-api' in seg:
                    continue
                out.append((cls.name, node.name, path, node.lineno,
                            not node.name.startswith('_')))
    return out


def main() -> int:
    prod_files = [p for p in _iter_py(REPO) if 'tests' not in p.parts]
    test_files = [p for p in _iter_py(REPO) if 'tests' in p.parts]

    prod_blob = '\n'.join(p.read_text(encoding='utf-8', errors='replace') for p in prod_files)
    test_blob = '\n'.join(p.read_text(encoding='utf-8', errors='replace') for p in test_files)

    methods = collect_methods(REPO)

    rows = []
    for cls, name, path, lineno, public in methods:
        # 定义在 tests/ 里的辅助类不是产品 API，跳过
        if 'tests' in path.parts:
            continue

        # 关键修正（第一版的两个系统性误报）：
        # 1) **函数引用传递**：`self.x.foo`（不带括号）作为回调/注册传出去，
        #    `.foo(` 匹配不到。`_dispatch_intelligence` 就是这么把
        #    `check_interlocks` 传进 `_safe_intel` 的 —— 第一版把它报成"生产零调用"。
        # 2) 只统计 `.foo(` 会漏掉「被同类以 self.foo 直接引用」的情况。
        # 所以两种形式都算消费方。
        call_pat = re.compile(r'\.' + re.escape(name) + r'\s*\(')
        ref_pat = re.compile(r'\.' + re.escape(name) + r'(?!\s*\()')
        prod_hits = len(call_pat.findall(prod_blob)) + len(ref_pat.findall(prod_blob))
        test_hits = len(call_pat.findall(test_blob)) + len(ref_pat.findall(test_blob))

        if prod_hits == 0:
            rows.append((public, cls, name, prod_hits, test_hits,
                         path.relative_to(REPO), lineno))

    rows.sort(key=lambda r: (not r[0], -r[4], str(r[5])))

    dead_both = [r for r in rows if r[4] == 0]
    test_only = [r for r in rows if r[4] > 0]

    print(f'扫描 {len(methods)} 个方法；生产侧无调用点 {len(rows)} 个'
          f'（完全死 {len(dead_both)} / 仅测试在用 {len(test_only)}）')

    print('\n=== 只有测试在用（生产零调用）—— 最可疑 ===')
    for public, cls, name, _p, th, rel, ln in test_only:
        mark = 'PUBLIC' if public else 'private'
        print(f'  {mark:7s} {cls}.{name:<34s} {rel}:{ln}  测试×{th}')

    print('\n=== 生产与测试都没调用（完全死）===')
    for public, cls, name, _p, _th, rel, ln in dead_both:
        mark = 'PUBLIC' if public else 'private'
        print(f'  {mark:7s} {cls}.{name:<34s} {rel}:{ln}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
