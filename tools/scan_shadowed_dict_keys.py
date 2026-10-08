"""扫描 `{…, **spread}` 里**被展开覆盖掉的显式键**。

透镜
----
Python 的 dict 字面量里，**后出现的键覆盖前面的**。所以：

    return {
        'queue_size': self.data_queue.qsize(),   # ← 实时算出来的
        **self.stats,                            # ← 里面也有 'queue_size'
    }

`self.stats` 里的那个会赢 —— **实时值被丢掉，而且不报错、不警告**。
返回的永远是 `self.stats['queue_size']` 那份**陈旧值**。

踩过（2026-10-08 SCADA round 194）
--------------------------------
`采集层/data_collector.py::get_stats()` 就是这么写的，`self.stats` 初始化里
带 `'queue_size': 0` → 实测：队列里放 5 条，`data_queue.qsize() == 5`
而 `get_stats()['queue_size'] == 0`。
**队列深度是监控上的「积压」指标，永远看不出来。**

口径（不收紧就不能用）
--------------------
* 只处理 `**self.<attr>` 这种展开形态（本仓库出现的唯一形态），
  外部对象 / 局部变量的展开**显式跳过并计数**，不假装扫干净了；
* `<attr>` 的键集合 = 同类的**字面量初始化** ∪ 同类的 **`self.<attr>['k'] = …` 写入**
  —— 后半条很重要：光删初始化里的键、留着后面的写入，缺陷会**在第一次批处理之后复活**；
* 只报「显式键出现在展开**之前**」的情形（那才是被覆盖的）。

用法
----
    python tools/scan_shadowed_dict_keys.py            # 人读
    python tools/scan_shadowed_dict_keys.py --json     # 机器可读
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PROD_DIRS = [
    "core", "展示层", "报警层", "采集层", "存储层", "智能层", "用户层",
    "timeseries", "gateway", "tools", "配置",
]
PROD_FILES = ["run.py", "launcher.py", "paths.py", "config.py"]

_SKIP_DIR_NAMES = {"__pycache__", ".venv", "node_modules", ".git", "tests"}


def _iter_py(root: Path):
    for p in root.rglob("*.py"):
        if any(part in _SKIP_DIR_NAMES for part in p.parts):
            continue
        if any(part.startswith("dist") or part.startswith("build") for part in p.parts):
            continue
        yield p


def _iter_dict_entries(node: ast.Dict):
    """逐个产出 ``(key_node_or_None, value_node)``。

    ⚠️ **`**x` 展开在 `values` 里，不在 `keys` 里**：对 `{'a': 1, **b}`，
    AST 是 ``keys=[Constant('a'), None]`` / ``values=[Constant(1), Name('b')]``。
    本工具第一版只看 `keys`，于是**一处展开都认不出来**，
    对已知有缺陷的代码也报「✅ 干净」—— 是个**瞎的守卫**。
    （是拿修复前的代码回放才发现的，见 tools 说明与 MEMORY 21t。）
    """
    for key, value in zip(node.keys, node.values):
        yield key, value


def _literal_str_keys(node: ast.Dict) -> set[str] | None:
    """取 dict 字面量里的字符串键；含非常量键时返回 None（表示"求不全"）。"""
    keys: set[str] = set()
    for k, _v in _iter_dict_entries(node):
        if k is None:  # `**spread`
            continue
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
        else:
            return None
    return keys


def _is_self_attr(node: ast.expr) -> str | None:
    """``self.<attr>`` → attr；否则 None。"""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


class _ClassAttrKeys(ast.NodeVisitor):
    """收集某个类里 `self.<attr>` 的静态键集合（初始化 + 后续下标写入）。"""

    def __init__(self, attr: str):
        self.attr = attr
        self.keys: set[str] = set()
        self.resolved = False

    def _absorb(self, value: ast.expr | None) -> None:
        """``self.<attr> = {...}``（含带注解的 AnnAssign）。"""
        if isinstance(value, ast.Dict):
            ks = _literal_str_keys(value)
            if ks is not None:
                self.keys |= ks
                self.resolved = True

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for t in node.targets:
            if _is_self_attr(t) == self.attr:
                self._absorb(node.value)
            # self.<attr>['k'] = ...
            if isinstance(t, ast.Subscript):
                if _is_self_attr(t.value) == self.attr:
                    sl = t.slice
                    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                        self.keys.add(sl.value)
                        self.resolved = True
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        # ⚠️ `self.stats: dict[str, Any] = {...}` 是 **AnnAssign** 不是 Assign。
        #    第一版只处理 Assign → 认不出 `self.stats` 的键集合 → 又是瞎的。
        if node.value is not None and _is_self_attr(node.target) == self.attr:
            self._absorb(node.value)
        self.generic_visit(node)


def _build_enclosing_map(tree: ast.AST) -> dict[int, ast.ClassDef | None]:
    """给每个节点记录它**所属的类**（没有则 None）。

    为什么需要：第一版只遍历 `ClassDef`，于是**类外**的 dict 字面量
    （模块级常量、独立函数里的返回）**根本没被扫** —— 又一个盲区。
    """
    enclosing: dict[int, ast.ClassDef | None] = {}

    def _walk(node: ast.AST, cls: ast.ClassDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            nxt = child if isinstance(child, ast.ClassDef) else cls
            enclosing[id(child)] = nxt
            _walk(child, nxt)

    for child in ast.iter_child_nodes(tree):
        nxt = child if isinstance(child, ast.ClassDef) else None
        enclosing[id(child)] = nxt
        _walk(child, nxt)
    return enclosing


def scan_file(path: Path) -> tuple[list[dict], int]:
    """返回 (findings, skipped_spread_count)。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return [], 0

    findings: list[dict] = []
    skipped = 0
    rel = path.relative_to(REPO).as_posix()

    enclosing = _build_enclosing_map(tree)
    key_cache: dict[tuple[int, str], set[str] | None] = {}

    def keys_for(cls: ast.ClassDef | None, attr: str) -> set[str] | None:
        """`cls` 里 `self.<attr>` 的静态键集合；求不出来返回 None。"""
        if cls is None:
            return None
        ck = (id(cls), attr)
        if ck not in key_cache:
            v = _ClassAttrKeys(attr)
            v.visit(cls)
            key_cache[ck] = v.keys if v.resolved else None
        return key_cache[ck]

    # 扫**全部** dict 字面量（含类外的）
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        cls = enclosing.get(id(node))
        explicit_before: list[tuple[str, int]] = []
        for k, v in _iter_dict_entries(node):
            if k is None:
                attr = _is_self_attr(v)
                ks = keys_for(cls, attr) if attr is not None else None
                if ks is None:
                    # 外部对象 / 局部变量 / 类外：本工具不求值 —— **显式计数**，
                    # 不假装扫干净了
                    skipped += 1
                    continue
                for name, lineno in explicit_before:
                    if name in ks:
                        findings.append({
                            "file": rel,
                            "class": cls.name if cls is not None else "<module>",
                            "spread": f"self.{attr}",
                            "key": name,
                            "line": lineno,
                            "spread_line": v.lineno,
                        })
                continue
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                explicit_before.append((k.value, k.lineno))
    return findings, skipped


def scan() -> dict:
    roots = [REPO / d for d in PROD_DIRS if (REPO / d).exists()]
    roots += [REPO / f for f in PROD_FILES if (REPO / f).exists()]

    findings: list[dict] = []
    skipped = 0
    files = 0
    for root in roots:
        for f in (_iter_py(root) if root.is_dir() else [root]):
            files += 1
            got, sk = scan_file(f)
            findings.extend(got)
            skipped += sk
    return {"files_scanned": files, "findings": findings,
            "unresolved_spreads_skipped": skipped}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = scan()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"扫描 {report['files_scanned']} 个生产 py 文件。")
    findings = report["findings"]
    if not findings:
        print("✅ 没有发现「被 **spread 覆盖掉的显式键」。")
    else:
        print(f"\n❌ 发现 {len(findings)} 处「显式键被后面的 **spread 覆盖」：")
        for f in findings:
            print(f"  {f['file']}:{f['line']}  {f['class']}  键 {f['key']!r} "
                  f"被 `**{f['spread']}`（第 {f['spread_line']} 行）覆盖")
        print("\n  说明：dict 字面量里**后出现的键覆盖前面的**，所以显式算出来的值会被丢掉，")
        print("        返回的是 spread 里那份陈旧值 —— 不报错，只是值永远是旧的。")
    if report["unresolved_spreads_skipped"]:
        print(f"\n⚠️ 有 {report['unresolved_spreads_skipped']} 处 `**self.<attr>` 的键集合"
              f"静态求不出来，已跳过（本扫描的已知漏报面）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
