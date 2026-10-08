#!/usr/bin/env python3
"""生产可达性分析（可复用模块）—— 供「接线声明守卫」与「死模块清单工具」共用。

为什么抽成模块而不是写在测试里
-----------------------------
同一套分析被两处需要：
  * `tests/test_wiring_declaration.py` —— 当**守卫**用（不可达必须声明）
  * `tools/report_dead_modules.py`    —— 当**清单**用（给 P2-1 决策提供精确数字）
写在两处必然漂移。**单一真源**是这里。

分析口径（每一条都是踩过之后定的，别随手改）
------------------------------------------
1. **根** = `run.py` / `launcher.py` / 含**模块级** `if __name__ == '__main__'` 语句的模块。
   ⚠️ 入口判定必须用 **AST**，不能用 `'__main__' in text` 子串匹配 ——
   注释/文档字符串里提到 `__main__` 的模块会被误判成入口（踩过：给模块补声明时，
   声明文本里正好含这几个字 → 那批模块瞬间"变成可达" → 结论整体翻转）。
2. **包名要进 BFS 队列**，不能只在遍历结束后补进结果集。
   ⚠️ 因为 `from core.version import X` 会先执行 `core/__init__.py`，
   而本项目 `core/__init__.py` 是 **eager import 一大堆子模块**的。
   踩过：事后补进 `seen` 导致一批**实际会被导入**的模块被误判"不可达"。
3. 排除 `tests/`、`tools/`（开发脚本）、`legacy/`、隐藏目录、`dist*`/`build*`（打包产物）。
   ⚠️ 打包产物目录按仓库约定命名（`dist-scada-*/`），只匹配 `dist` 会漏 —— 踩过。
"""

from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass, field

MARKER_RE = re.compile(r"接线状态\s*[:：]\s*未接线")

SKIP_DIRS = {"tests", "tools", "legacy", "测试", "build", "dist", "node_modules"}


def _included(root: pathlib.Path, p: pathlib.Path) -> bool:
    rel = str(p.relative_to(root)).replace("\\", "/")
    parts = rel.split("/")
    if any(part.startswith(".") for part in parts[:-1]):
        return False
    head = parts[0]
    if head in SKIP_DIRS:
        return False
    if head.startswith(("dist", "build")):
        return False

    # ⚠️ **不能扫「别人仓库」的文件**。
    # CI 的 test job 会把前端仓库 checkout 到 `<workspace>/scada-app`
    # （给契约测试用），于是 `rglob("*.py")` 会扫到 `scada-app/backend/build_entry.py`
    # → 被判成"不可达且未声明" → **CI 红、本机绿**（本机没有那个 checkout）。
    # 实测就是这样红的（annotation 原文：`scada-app.backend.build_entry`）。
    #
    # 判据：路径上任何一级目录若**自己是 git 仓库根**（含 `.git`），或**是一个
    # JS 工程根**（含 `package.json`），就整棵跳过 —— 那不属于本仓库的产品代码。
    for i in range(len(parts) - 1):
        d = root.joinpath(*parts[: i + 1])
        if (d / ".git").exists() or (d / "package.json").is_file():
            return False
    return True


def _modname(root: pathlib.Path, p: pathlib.Path) -> str:
    parts = list(p.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def is_entry_point(path: pathlib.Path) -> bool:
    """是否含**模块级** `if __name__ == '__main__':` 语句（AST 精确判定）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return False
    for node in tree.body:
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        left = node.test.left
        if not (isinstance(left, ast.Name) and left.id == "__name__"):
            continue
        for op, comp in zip(node.test.ops, node.test.comparators):
            if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant) and comp.value == "__main__":
                return True
    return False


def _imports_of(root: pathlib.Path, path: pathlib.Path, known: set[str]) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set()
    out: set[str] = set()

    def add(name: str | None) -> None:
        if not name:
            return
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in known:
                out.add(cand)
                return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = _modname(root, path.parent / "__init__.py") if path.parent != root else ""
                base = pkg
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                add(f"{base}.{node.module}" if node.module else base)
            else:
                add(node.module)
                for a in node.names:
                    add(f"{node.module}.{a.name}" if node.module else a.name)
    return out


def api_names(path: pathlib.Path) -> set[str]:
    """模块顶层公开的类/函数名（不含下划线开头）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set()
    return {
        n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not n.name.startswith("_")
    }


@dataclass
class WiringReport:
    path_of: dict[str, pathlib.Path] = field(default_factory=dict)
    unreachable: set[str] = field(default_factory=set)
    declared: dict[str, bool] = field(default_factory=dict)

    @property
    def undeclared(self) -> list[str]:
        return sorted(m for m in self.unreachable if not self.declared.get(m, False))

    @property
    def line_count(self) -> int:
        total = 0
        for m in self.unreachable:
            total += len(self.path_of[m].read_text(encoding="utf-8", errors="ignore").splitlines())
        return total

    def classify(self) -> tuple[list[tuple[str, int, dict]], list[tuple[str, int, list[str]]]]:
        """分成两类：**有活替代**（API 名字在活模块里也有）与**能力孤本**。"""
        live_api: dict[str, list[str]] = {}
        for m, p in self.path_of.items():
            if m in self.unreachable:
                continue
            for n in api_names(p):
                live_api.setdefault(n, []).append(m)

        dup_rows, orphan_rows = [], []
        for m in sorted(self.unreachable):
            p = self.path_of[m]
            lines = len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            names = api_names(p)
            dup = {n: live_api[n][:2] for n in names if n in live_api}
            if dup:
                dup_rows.append((m, lines, dup))
            else:
                orphan_rows.append((m, lines, sorted(names)[:4]))
        return dup_rows, orphan_rows


def analyse(root: pathlib.Path) -> WiringReport:
    files = [p for p in root.rglob("*.py") if _included(root, p)]
    path_of = {_modname(root, p): p for p in files}
    known = set(path_of)
    graph = {m: _imports_of(root, p, known) for m, p in path_of.items()}

    roots = ["run", "launcher"]
    roots += [m for m, p in path_of.items() if is_entry_point(p)]
    # 包名进队列（见模块 docstring 第 2 条）
    for m in path_of:
        parts = m.split(".")
        for i in range(1, len(parts)):
            roots.append(".".join(parts[:i]))

    seen: set[str] = set()
    stack = sorted(set(roots))
    while stack:
        m = stack.pop()
        if m in seen or m not in graph:
            continue
        seen.add(m)
        stack.extend(graph[m] - seen)

    unreachable = {m for m in graph if m not in seen}
    declared = {
        m: bool(MARKER_RE.search(path_of[m].read_text(encoding="utf-8", errors="ignore")))
        for m in unreachable
    }
    return WiringReport(path_of=path_of, unreachable=unreachable, declared=declared)
