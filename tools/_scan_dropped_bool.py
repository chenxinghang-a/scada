# -*- coding: utf-8 -*-
"""静态扫「返回 bool 的函数被当语句调用、结果被丢」。

round 302/303 修的假成功里，这一类反复出现：
`_save_config()` / `add_device()` / `connect()` 的返回值没人看，
失败时接口照样回「已保存」「已连接」。

判据（AST）：
- 第一遍收集仓库里**声明返回 bool**（注解）或**含 `return True/False` 字面量**的函数；
- 第二遍找「表达式语句 = 一次调用」且被调函数名命中上一步集合的位置。

会有误报（库函数、不同模块同名函数），所以结果必须人工筛。
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SKIP = {'dist', 'backup', 'backups', 'build', 'node_modules', '.venv', '.git',
        '__pycache__', '.pytest_cache', '.vitest_cache', '.build-backend',
        'release', 'evidence', 'exports', '.ui-shots'}

STDLIB_OK = {
    'makedirs', 'mkdir', 'setdefault', 'update', 'add', 'append', 'extend',
    'write', 'flush', 'close', 'commit', 'execute', 'executemany', 'sleep',
    'join', 'start', 'acquire', 'release', 'clear', 'remove', 'discard',
    'sort', 'set', 'dump', 'load', 'info', 'warning', 'error', 'debug',
    'exception', 'critical', 'print', 'format', 'strip', 'replace', 'split',
    'get', 'pop', 'keys', 'values', 'items', 'isdigit', 'isalpha',
}


def files(root):
    for p in root.rglob('*.py'):
        if not set(p.parts) & SKIP:
            yield p


def main() -> int:
    bool_funcs = defaultdict(list)      # name -> [位置]
    ast_files = {}

    for p in files(REPO):
        try:
            src = p.read_text(encoding='utf-8')
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        ast_files[p] = tree
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            returns_bool = False
            if node.returns is not None and 'bool' in ast.dump(node.returns):
                returns_bool = True
            if not returns_bool:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant) \
                            and isinstance(sub.value.value, bool):
                        returns_bool = True
                        break
            if returns_bool:
                bool_funcs[node.name].append(f'{p.relative_to(REPO)}:{node.lineno}')

    hits = []
    for p, tree in ast_files.items():
        lines = (REPO / p).read_text(encoding='utf-8').splitlines() if isinstance(p, Path) else []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            # 取被调用的名字（属性取 attr，名字取 id）
            name = None
            if isinstance(call.func, ast.Attribute):
                name = call.func.attr
            elif isinstance(call.func, ast.Name):
                name = call.func.id
            if not name or name not in bool_funcs or name in STDLIB_OK:
                continue
            try:
                line = lines[node.lineno - 1].strip()
            except Exception:
                line = ''
            hits.append((name, f'{p.relative_to(REPO)}:{node.lineno}', line))

    hits.sort(key=lambda h: h[0])
    print(f'返回 bool 的函数 {len(bool_funcs)} 个；结果被丢弃的调用点 {len(hits)} 处\n')
    for name, loc, line in hits:
        print(f'{loc:52s} {name:<26s} {line[:90]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
