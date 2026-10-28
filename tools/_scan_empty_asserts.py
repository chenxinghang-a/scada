# -*- coding: utf-8 -*-
"""扫描 tests/ 里的「空测」：断言被包在「可能为假」的条件里，于是没执行也通过。

四类模式：
  A) if <mock>.called:  /  if <mock>.call_count:  —— 块内断言只在调用发生时才校验
  B) try/except: pass 包住断言
  C) 恒真断言（同一个东西自己比、len(...) >= 0）
  D) 整个测试函数没有任何失败信号（assert / pytest.raises / mock.assert_*）

D 类允许显式豁免：在函数体内任意位置写 `empty-test-ok`（通常配一段 docstring
说明为什么只验证「不抛」）。豁免必须写理由，否则下一轮没法判断是不是偷懒。

既可直接跑（打印报告），也可被 tests/test_empty_assert_guard.py 导入调用。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SMOKE_MARK = 'empty-test-ok'


def _called_like(node: ast.AST) -> bool:
    """判断表达式是否形如 x.called / x.call_count / x.call_count > 0。"""
    if isinstance(node, ast.Attribute) and node.attr in ('called', 'call_count'):
        return True
    if isinstance(node, ast.Compare):
        return _called_like(node.left) or any(_called_like(c) for c in node.comparators)
    if isinstance(node, ast.UnaryOp):
        return _called_like(node.operand)
    return False


def _has_assert(nodes) -> bool:
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Assert):
                return True
    return False


def _const_compare(node: ast.Assert) -> str | None:
    """找出形如 assert x >= 0 / assert x == x 的恒真（或近乎恒真）断言。"""
    t = node.test
    if not isinstance(t, ast.Compare) or len(t.ops) != 1:
        return None
    left, op, right = t.left, t.ops[0], t.comparators[0]

    if (isinstance(left, ast.Name) and isinstance(right, ast.Name)
            and left.id == right.id):
        return 'assert x <op> x（恒真）'

    if isinstance(right, ast.Constant) and right.value == 0:
        if isinstance(op, (ast.GtE, ast.LtE)) and isinstance(left, ast.Call):
            fn = left.func
            if isinstance(fn, ast.Name) and fn.id == 'len':
                return 'assert len(...) >= 0（恒真）'
    return None


def _has_failure_signal(fn: ast.FunctionDef) -> bool:
    """函数体内是否存在任何「会失败」的东西。"""
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Assert):
            return True
        if isinstance(sub, ast.With):
            for item in sub.items:
                ctx = item.context_expr
                if (isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute)
                        and ctx.func.attr == 'raises'):
                    return True
        if isinstance(sub, ast.Call):
            fn_node = sub.func
            if isinstance(fn_node, ast.Attribute):
                # mock 的 assert_called_once_with / assert_not_called 等
                # 同样会抛 AssertionError，必须算失败信号
                if fn_node.attr.startswith('assert'):
                    return True
                if fn_node.attr in ('fail', 'raises'):
                    return True
            if isinstance(fn_node, ast.Name) and fn_node.id == 'fail':
                return True
    return False


def _iter_test_funcs(tree: ast.Module):
    """只产出真正的测试函数：模块级 def test_* 与类里的 def test_*。

    不能用 ast.walk 全树扫 FunctionDef —— 测试内部常定义同名的辅助函数
    （例如 `def test_check(): ...` 作为被注册的回调），那样会把辅助函数
    误报成「没有断言的测试」。
    """
    for top in tree.body:
        if isinstance(top, ast.FunctionDef) and top.name.startswith('test'):
            yield top
        elif isinstance(top, ast.ClassDef):
            for member in top.body:
                if isinstance(member, ast.FunctionDef) and member.name.startswith('test'):
                    yield member


def scan(tests_dir: Path | None = None) -> dict[str, list[str]]:
    """返回 {类别: [问题描述]}；D 类里已声明 smoke 的单独归到 D_declared。"""
    tests_dir = tests_dir or (REPO / 'tests')
    issues: dict[str, list[str]] = {'A': [], 'B': [], 'C': [], 'D': [], 'D_declared': []}

    for path in sorted(tests_dir.glob('*.py')):
        try:
            source = path.read_text(encoding='utf-8')
            tree = ast.parse(source)
        except SyntaxError as e:
            issues['A'].append(f'{path.name}: 语法错误 {e}')
            continue
        lines = source.splitlines()

        for node in ast.walk(tree):
            # A) if <mock>.called: 块内有断言
            if isinstance(node, ast.If):
                if _called_like(node.test) and not isinstance(node.test, ast.UnaryOp):
                    if _has_assert(node.body):
                        issues['A'].append(
                            f'{path.name}:{node.lineno}  if {ast.unparse(node.test)}:  <断言>')

            # B) try: <断言> except: pass
            if isinstance(node, ast.Try) and _has_assert(node.body):
                for handler in node.handlers:
                    if all(isinstance(s, ast.Pass) for s in handler.body):
                        issues['B'].append(
                            f'{path.name}:{node.lineno}  try: <断言> except: pass')

            # C) 恒真断言
            if isinstance(node, ast.Assert):
                tag = _const_compare(node)
                if tag:
                    issues['C'].append(f'{path.name}:{node.lineno}  {tag}')

        # D) 整个测试函数没有任何失败信号（单独一轮，避免被嵌套 def 干扰）
        for fn in _iter_test_funcs(tree):
            if _has_failure_signal(fn):
                continue
            seg = '\n'.join(lines[fn.lineno - 1:fn.end_lineno or fn.lineno])
            entry = f'{path.name}:{fn.lineno}  {fn.name}()'
            if SMOKE_MARK in seg:
                issues['D_declared'].append(entry)
            else:
                issues['D'].append(entry)

    return issues


def main() -> int:
    issues = scan()
    for key, title in (('A', '断言被 if <mock>.called 包住'),
                       ('B', '断言被 try/except:pass 包住'),
                       ('C', '恒真断言'),
                       ('D', '测试函数内没有任何 assert / raises（未声明豁免）'),
                       ('D_declared', '已声明 empty-test-ok 的 smoke（放行）')):
        print(f'\n=== {title}（{len(issues[key])}）===')
        for line in issues[key][:40]:
            print('  ' + line)
        if len(issues[key]) > 40:
            print(f'  ... 另有 {len(issues[key]) - 40} 条')

    blocking = sum(len(issues[k]) for k in ('A', 'B', 'C', 'D'))
    print(f'\n需要处理 {blocking} 处；已豁免 smoke {len(issues["D_declared"])} 处')
    return 0


if __name__ == '__main__':
    sys.exit(main())
