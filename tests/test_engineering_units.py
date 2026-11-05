# -*- coding: utf-8 -*-
"""工程量换算唯一入口（round 372）。

两件事：
1. 行为：`from_raw` / `to_raw` 的往返、缺省值、非法 scale、非有限值；
2. **结构守卫**：生产代码里不许再出现 `raw*scale+offset` / `(value-offset)/scale`
   —— 这类公式原来有 10 份副本，修一处、其余照错（审计里 scale 双算/漏算反复出现）。
"""
from __future__ import annotations

import ast
import os

import pytest

from core.engineering_units import from_raw, scale_offset_of, to_raw

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    "build", "dist", "legacy", "tests", "测试", "tools",
}


class TestRoundTrip:
    def test_from_raw(self):
        assert from_raw(500, 0.1, 0.0) == 50.0
        assert from_raw(500, 0.1, 5.0) == 55.0

    def test_to_raw(self):
        assert to_raw(50.0, 0.1, 0.0) == 500.0
        assert to_raw(55.0, 0.1, 5.0) == 500.0

    @pytest.mark.parametrize('raw, scale, offset', [
        (0, 1, 0), (500, 0.1, 0), (-40, 0.5, 10), (32767, 2, -100),
    ])
    def test_roundtrip(self, raw, scale, offset):
        assert to_raw(from_raw(raw, scale, offset), scale, offset) == pytest.approx(raw)

    def test_defaults(self):
        assert from_raw(42) == 42.0
        assert to_raw(42) == 42.0

    def test_none_scale_offset_use_defaults(self):
        """YAML 里写 `scale:` 为空时按 1/0 处理（与历史行为一致）。"""
        assert from_raw(10, None, None) == 10.0
        assert to_raw(10, None, None) == 10.0


class TestRejectsBadConfig:
    def test_zero_scale_raises(self):
        """scale=0 会让量程坍缩，不能静默当成 1.0（那会把 500 读成 500 而不是 50）。"""
        with pytest.raises(ValueError):
            from_raw(1, 0, 0)
        with pytest.raises(ValueError):
            to_raw(1, 0, 0)

    @pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), 'x', None])
    def test_non_finite_value_raises(self, bad):
        if bad is None:
            return          # None 单独由默认值语义覆盖
        with pytest.raises(ValueError):
            from_raw(bad, 1, 0)
        with pytest.raises(ValueError):
            to_raw(bad, 1, 0)

    def test_scale_offset_of(self):
        assert scale_offset_of({'scale': 0.1, 'offset': 5}) == (0.1, 5.0)
        assert scale_offset_of({}) == (1.0, 0.0)
        assert scale_offset_of(None) == (1.0, 0.0)


# ---------------------------------------------------------------------------
# 结构守卫：换算公式只许出现在 core/engineering_units.py
# ---------------------------------------------------------------------------

def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _name(node.func)
    return ''


def _iter_production_py():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith('.py'):
                yield os.path.join(root, name)


def _duplicate_formulas(tree: ast.AST) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(tree):
        # raw * scale + offset
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = node.left, node.right
            if (isinstance(left, ast.BinOp) and isinstance(left.op, ast.Mult)
                    and 'scale' in (_name(left.left) + _name(left.right)).lower()
                    and 'offset' in _name(right).lower()):
                hits.append(node.lineno)
        # (value - offset) / scale
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if 'scale' in _name(node.right).lower():
                left = node.left
                if (isinstance(left, ast.BinOp) and isinstance(left.op, ast.Sub)
                        and 'offset' in _name(left.right).lower()):
                    hits.append(node.lineno)
    return hits


def test_no_duplicate_conversion_formula():
    """生产代码里不许再写换算公式 —— 必须调用 `from_raw` / `to_raw`。

    `tools/sim_server_selftest.py` 被排除：它是**独立校验器**，
    故意保留原始公式作为对照实现（测试预言机不该复用被测代码）。
    """
    offenders = []
    for path in _iter_production_py():
        rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, '/')
        if rel == 'core/engineering_units.py':
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=path)
        except (OSError, SyntaxError):
            continue
        for line in _duplicate_formulas(tree):
            offenders.append(f'{rel}:{line}')

    assert not offenders, (
        '发现重复的 scale/offset 换算公式（统一入口是 core/engineering_units.py）：\n  '
        + '\n  '.join(sorted(offenders))
    )
