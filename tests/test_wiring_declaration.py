"""接线声明守卫：**生产不可达的模块必须显式声明「未接线」**。

为什么需要这条
-------------
本仓库反复被同一类问题咬：**「代码在库里」被误读成「功能在跑」**。
- round 175：前端读 `<backend_dir>/data/`，后端写 `<backend_dir>/_internal/data/`
  → 那个「避免端口错配」的机制**从未生效过**
- round 184：`采集层/device_manager.py` 的 `DeviceManager` 生产不可达却被 6+ 测试覆盖
- 更早：一批 core 模块被测试覆盖、看起来活着，实际没有任何调用方

所以仓库里立了一条约定：这类模块在文件头写
`# 接线状态：未接线（WIRED = False）`。**本守卫把这条约定变成机械可查的。**

判据（可复现、不依赖机器状态）
-----------------------------
1. 以 `run.py` / `launcher.py` / **任何含 `if __name__ == '__main__'` 的模块**为根，
   沿 import 图（含函数内局部导入）做可达性分析
2. 包的 `__init__.py` 只要其下任一子模块可达，就视为可达（Python 隐式导入）
3. 不可达的模块**必须**带接线声明；否则它就是**未声明的死代码** —— 报红

`tools/`（开发脚本）与 `tests/` 不参与：它们本就不该被 `run.py` 引用。

⚠️ 静态可达性分析**天然有假阳性**（动态 import、字符串拼模块名、插件注册表）。
所以留了 `ALLOWLIST`：确有"静态看不见的接线方式"的模块登记在此，**并写明理由**。
新增条目必须先解释清楚 —— 这正是本守卫想逼出来的那个"有意识的决定"。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {"tests", "tools", "legacy", "测试", "build", "dist", "node_modules"}
MARKER = re.compile(r"接线状态\s*[:：]\s*未接线")

# 静态分析看不见接线方式的模块（登记时必须写理由）。
ALLOWLIST: dict[str, str] = {}


def _included(p: pathlib.Path) -> bool:
    rel = str(p.relative_to(ROOT)).replace("\\", "/")
    if any(part.startswith(".") for part in rel.split("/")[:-1]):
        return False
    head = rel.split("/")[0]
    if head in SKIP_DIRS:
        return False
    # 打包产物目录按仓库既有约定命名（`dist-scada-*/`、`build-scada-*/`）——
    # 只匹配 `dist` / `build` 会漏掉它们（第一版就漏了，扫出一堆
    # `dist-scada-1.3.1037.…` 的假阳性）。
    return not head.startswith(("dist", "build"))


def _modname(p: pathlib.Path) -> str:
    parts = list(p.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports_of(path: pathlib.Path, known: set[str]) -> set[str]:
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
                pkg = _modname(path.parent / "__init__.py") if path.parent != ROOT else ""
                base = pkg
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                add(f"{base}.{node.module}" if node.module else base)
            else:
                add(node.module)
                for a in node.names:
                    add(f"{node.module}.{a.name}" if node.module else a.name)
    return out


def _is_entry_point(path: pathlib.Path) -> bool:
    """是否含**模块级** `if __name__ == '__main__':` 语句。

    ⚠️ 必须用 AST，不能用 `'__main__' in text` 那种子串判断 ——
    第一版就是子串判断，结果**注释/文档字符串里提到 `__main__` 的模块**
    也被当成入口。实测后果：给模块补「未接线」声明时，声明文本里正好写了
    `if __name__ == '__main__'` 这几个字 → 那些模块瞬间"变成可达" → 整个结论翻转。
    **判据必须落在语法结构上，不能落在文本上。**
    """
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


def _analyse() -> tuple[dict[str, pathlib.Path], set[str]]:
    files = [p for p in ROOT.rglob("*.py") if _included(p)]
    path_of = {_modname(p): p for p in files}
    known = set(path_of)

    graph = {m: _imports_of(p, known) for m, p in path_of.items()}

    roots = ["run", "launcher"]
    roots += [m for m, p in path_of.items() if _is_entry_point(p)]

    # 包名也要进 BFS 队列（**不是事后补进 seen**）：
    # Python 里 `from core.version import X` 会先执行 `core/__init__.py`，
    # 而本仓库的 `core/__init__.py` 是 **eager import 一大堆子模块**的。
    # 第一版把包名事后加进 seen、没再展开它的导入 → 一大批**实际会被导入**的模块
    # 被误判成"不可达"（据此还错加了 24 个声明，已撤回）。
    for m in path_of:
        parts = m.split(".")
        for i in range(1, len(parts)):
            roots.append(".".join(parts[:i]))
    roots = sorted(set(roots))

    seen: set[str] = set()
    stack = list(roots)
    while stack:
        m = stack.pop()
        if m in seen or m not in graph:
            continue
        seen.add(m)
        stack.extend(graph[m] - seen)

    unreachable = {m for m in graph if m not in seen}
    return path_of, unreachable


PATH_OF, UNREACHABLE = _analyse()


def test_analysis_is_sane():
    """守卫自身的前置对照：分析必须真的在跑，且结论不是"全都可达/全都不可达"。"""
    assert len(PATH_OF) > 100, f"只扫到 {len(PATH_OF)} 个模块，分析口径可能坏了"
    assert "run" in PATH_OF, "入口 run.py 没被扫到"
    assert UNREACHABLE, "一个不可达模块都没有 —— 分析口径可能坏了（假绿）"
    assert len(UNREACHABLE) < len(PATH_OF), "全部不可达 —— 分析口径可能坏了"


def test_unreachable_modules_declare_wiring_status():
    """生产不可达的模块必须显式声明「未接线」，否则就是未声明的死代码。"""
    missing = sorted(
        m for m in UNREACHABLE
        if m not in ALLOWLIST and not MARKER.search(PATH_OF[m].read_text(encoding="utf-8", errors="ignore"))
    )
    assert not missing, (
        "以下模块**生产代码不可达**、却**没有**接线声明 —— "
        "它们会被误读成「功能在跑」：\n"
        + "\n".join(f"  {m}  ({PATH_OF[m].relative_to(ROOT)})" for m in missing)
        + "\n两种处理：① 在文件头补 `# 接线状态：未接线（WIRED = False）`；"
          "② 若它其实是被动态/字符串方式接线的，登记进 ALLOWLIST 并写明理由。"
    )


def test_allowlist_entries_are_still_unreachable_and_justified():
    """登记表不能腐烂：已登记的模块若变成可达（或带上了声明），必须从表里删掉。"""
    stale = []
    for m, reason in ALLOWLIST.items():
        if m not in PATH_OF:
            stale.append(f"{m}: 模块已不存在")
            continue
        if m not in UNREACHABLE:
            stale.append(f"{m}: 现在已可达 —— 请从 ALLOWLIST 删除")
            continue
        if MARKER.search(PATH_OF[m].read_text(encoding="utf-8", errors="ignore")):
            stale.append(f"{m}: 已带接线声明 —— 请从 ALLOWLIST 删除")
            continue
        if not reason.strip():
            stale.append(f"{m}: 没写理由")
    assert not stale, "ALLOWLIST 需要清理：\n" + "\n".join(f"  {s}" for s in stale)


# 「**可达**却仍标着未接线」的已知历史遗留。
# 这类通常意味着：模块能被 import（例如被某个包的 __init__ 顺带导入），
# 但能力并未被真正调用 —— 声明的**措辞**与事实不符，需要逐个核实后改口径。
# 登记在此是为了**把事实露出来**，而不是用 skip 藏起来。
REACHABLE_BUT_MARKED_KNOWN = {
    "core.generate_certs": "被 core/__init__ 间接导入；是否有调用方待核实",
}


def test_wired_modules_do_not_carry_stale_declaration():
    """反向：**可达**的模块不该还挂着「未接线」声明（要么接线了忘删，要么措辞不符）。"""
    stale = sorted(
        m for m in PATH_OF
        if m not in UNREACHABLE
        and m not in REACHABLE_BUT_MARKED_KNOWN
        and MARKER.search(PATH_OF[m].read_text(encoding="utf-8", errors="ignore"))
    )
    assert not stale, (
        "以下模块**生产可达**、却仍标着「未接线」—— 声明与事实不符：\n"
        + "\n".join(f"  {m}  ({PATH_OF[m].relative_to(ROOT)})" for m in stale)
        + "\n要么接线了忘删声明（删掉声明块），要么是'能 import 但没调用'"
          "（改措辞或登记进 REACHABLE_BUT_MARKED_KNOWN 并写明理由）。"
    )


def test_reachable_but_marked_known_entries_are_still_valid():
    """登记表不能腐烂：条目若变成不可达（或删了声明），必须从表里去掉。"""
    bad = []
    for m, reason in REACHABLE_BUT_MARKED_KNOWN.items():
        if m not in PATH_OF:
            bad.append(f"{m}: 模块已不存在")
        elif m in UNREACHABLE:
            bad.append(f"{m}: 现在是**不可达**（应走接线声明那条路）—— 请从本表删除")
        elif not MARKER.search(PATH_OF[m].read_text(encoding="utf-8", errors="ignore")):
            bad.append(f"{m}: 已无接线声明 —— 请从本表删除")
        elif not reason.strip():
            bad.append(f"{m}: 没写理由")
    assert not bad, "REACHABLE_BUT_MARKED_KNOWN 需要清理：\n" + "\n".join(f"  {b}" for b in bad)
