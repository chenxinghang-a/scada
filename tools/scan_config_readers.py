"""扫描 config.py 里定义的配置项，找出「算了但没人读」的字段。

透镜（MEMORY 硬规则 17）：
    对每个「算出来存起来的配置字段，grep 它的读取点，为 0 就是嫌疑」。

用法：
    python tools/scan_config_readers.py            # 人读的清单
    python tools/scan_config_readers.py --json     # 机器可读

口径：
    * 「读取点」= 生产代码里的 `Cls.ATTR` 或 `config.Cls.ATTR`（AST 精确，不用子串匹配）。
    * 测试代码里的读取**单独统计** —— 只被测试读的配置项，在生产里等于没人读。
    * `getattr(Cls, ...)` / `Cls.__dict__` 这类动态访问会让扫描**漏报**，
      所以脚本会显式报告「存在动态访问」的位置，供人工判断，而不是假装扫干净了。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO / "config.py"

# 生产代码目录（与 tools/wiring_analysis.py 的口径保持一致：不含测试、不含构建产物）
PROD_DIRS = [
    "core",
    "展示层",
    "报警层",
    "采集层",
    "存储层",
    "智能层",
    "用户层",
    "timeseries",
    "gateway",
    "tools",
    "配置",
]
PROD_FILES = ["run.py", "launcher.py", "paths.py"]

TEST_DIR = REPO / "tests"

_SKIP_DIR_NAMES = {"__pycache__", ".venv", "node_modules", ".git"}


def _iter_py(root: Path):
    for p in root.rglob("*.py"):
        if any(part in _SKIP_DIR_NAMES for part in p.parts):
            continue
        if any(part.startswith("dist") or part.startswith("build") for part in p.parts):
            continue
        yield p


def collect_config_classes() -> dict[str, dict[str, int]]:
    """从 config.py 取 {类名: {属性名: 行号}}（只看类体顶层的简单赋值）。"""
    tree = ast.parse(CONFIG_FILE.read_text(encoding="utf-8"))
    out: dict[str, dict[str, int]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        attrs: dict[str, int] = {}
        for stmt in node.body:
            targets = []
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets = [stmt.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    attrs[t.id] = stmt.lineno
        if attrs:
            out[node.name] = attrs
    return out


def collect_class_aliases(classes: dict[str, dict[str, int]]) -> dict[str, str]:
    """收集 config.py 里的**类别名**：``WebConfig = FlaskConfig`` → {"WebConfig": "FlaskConfig"}。

    为什么必须处理别名
    ------------------
    代码里 `run.py:510` 读的是 `WebConfig.HOST`，而类名是 `FlaskConfig`。
    旧版扫描器只认 `Cls.ATTR` 的字面类名，于是把 `FlaskConfig.HOST/PORT/REAL_PORT`
    全报成「无人读」——**假阳性**。

    假阳性的代价不是「多几行输出」，而是**这份清单会被人整体忽略**：
    一份 56 项里混着 7 项本来就有人读的报告，没人会去逐条核对，
    真正的「算了没人读」就被淹掉了（这正是本项目反复栽的「闸门喊狼来了」）。
    """
    tree = ast.parse(CONFIG_FILE.read_text(encoding="utf-8"))
    aliases: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if isinstance(tgt, ast.Name) and isinstance(node.value, ast.Name):
            if node.value.id in classes and tgt.id not in classes:
                aliases[tgt.id] = node.value.id
    return aliases


def scan_readers(resolve: dict[str, str], roots: list[Path]):
    """返回 (读取点, 动态访问点)。读取点形状 {(cls, attr): [文件:行, ...]}。

    ``resolve`` 是「名字 → 规范类名」的映射，**包含类名自身与全部别名**，
    这样 `WebConfig.HOST` 会被记到 `FlaskConfig.HOST` 名下。
    """
    readers: dict[tuple[str, str], list[str]] = {}
    dynamic: list[str] = []

    for root in roots:
        files = _iter_py(root) if root.is_dir() else [root]
        for f in files:
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            rel = f.relative_to(REPO).as_posix()
            for node in ast.walk(tree):
                # Cls.ATTR / Alias.ATTR
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    canonical = resolve.get(node.value.id)
                    if canonical is not None:
                        readers.setdefault((canonical, node.attr), []).append(
                            f"{rel}:{node.lineno}"
                        )
                # config.Cls.ATTR / config.Alias.ATTR
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
                    inner = node.value
                    if (
                        isinstance(inner.value, ast.Name)
                        and inner.value.id == "config"
                        and resolve.get(inner.attr) is not None
                    ):
                        readers.setdefault((resolve[inner.attr], node.attr), []).append(
                            f"{rel}:{node.lineno}"
                        )
                # getattr(Cls, ...) / Cls.__dict__
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id == "getattr" and node.args:
                        a0 = node.args[0]
                        if isinstance(a0, ast.Name) and resolve.get(a0.id) is not None:
                            dynamic.append(f"{rel}:{node.lineno} getattr({a0.id}, ...)")
                        elif (
                            isinstance(a0, ast.Attribute)
                            and isinstance(a0.value, ast.Name)
                            and a0.value.id == "config"
                            and resolve.get(a0.attr) is not None
                        ):
                            dynamic.append(f"{rel}:{node.lineno} getattr(config.{a0.attr}, ...)")
                elif isinstance(node, ast.Attribute) and node.attr == "__dict__":
                    if isinstance(node.value, ast.Name) and resolve.get(node.value.id) is not None:
                        dynamic.append(f"{rel}:{node.lineno} {node.value.id}.__dict__")
    return readers, dynamic


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    classes = collect_config_classes()
    aliases = collect_class_aliases(classes)
    # 名字 → 规范类名：类名映射到自身，别名映射到它指向的类
    resolve = {name: name for name in classes}
    resolve.update(aliases)

    prod_roots = [REPO / d for d in PROD_DIRS if (REPO / d).exists()]
    prod_roots += [REPO / f for f in PROD_FILES if (REPO / f).exists()]

    prod_readers, dynamic = scan_readers(resolve, prod_roots)
    test_readers, _ = scan_readers(resolve, [TEST_DIR])

    report = {"classes": {}, "dynamic_access": dynamic, "aliases": aliases}
    for cls, attrs in sorted(classes.items()):
        rows = []
        for attr, lineno in sorted(attrs.items(), key=lambda kv: kv[1]):
            prod = prod_readers.get((cls, attr), [])
            test = test_readers.get((cls, attr), [])
            if prod:
                verdict = "read"
            elif test:
                verdict = "test-only"
            else:
                verdict = "UNREAD"
            rows.append(
                {
                    "attr": attr,
                    "lineno": lineno,
                    "verdict": verdict,
                    "prod_readers": prod,
                    "test_readers": test,
                }
            )
        report["classes"][cls] = rows

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    unread_total = 0
    for cls, rows in report["classes"].items():
        unread = [r for r in rows if r["verdict"] != "read"]
        if not unread:
            continue
        print(f"\n=== {cls} ({len(rows)} 项，其中 {len(unread)} 项生产代码不读) ===")
        for r in unread:
            tag = "❌ 无人读" if r["verdict"] == "UNREAD" else "⚠️ 仅测试读"
            unread_total += 1
            print(f"  {tag}  {cls}.{r['attr']}  (config.py:{r['lineno']})")
            for t in r["test_readers"][:3]:
                print(f"            测试读点: {t}")

    print(f"\n合计 {unread_total} 项在生产代码里没有读取点。")
    if aliases:
        pairs = ", ".join(f"{a} → {c}" for a, c in sorted(aliases.items()))
        print(f"\n已解析类别名 {len(aliases)} 个（别名下的读取已计入规范类名）：{pairs}")
    if dynamic:
        print(f"\n⚠️ 发现 {len(dynamic)} 处动态访问（可能造成漏报，需人工判断）：")
        for d in dynamic:
            print(f"    {d}")
    else:
        print("\n（无动态访问、别名已解析 → 本扫描没有已知漏报通道）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
