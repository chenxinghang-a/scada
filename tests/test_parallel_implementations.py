"""「平行实现」登记表 —— 定义了公开类、但**生产代码里零实例化**的模块。

缺陷类别：**平行实现看起来是活的**
----------------------------------
本仓反复被「代码在库里 ⇒ 功能在跑」咬。本文件钉的是其中一类：
模块**可 import、被包 `__init__` re-export、还有专门的单测**，
但生产装配链（`run.py` 及其 import 图）里**从来没有实例化它的公开类** ——
真正在跑的，是**另一处**实现同一能力的代码。

为什么现有的两个守卫都看不见它：
* `tests/test_wiring_declaration.py` 判的是「**可达性**」（能不能被 import 到）。
  被 `__init__.py` re-export 的模块**必然可达** —— 于是永远不算「未接线」。
* `test_readme_accuracy.py` 只查 README 目录树里的文件是否存在。

所以需要第三个口径：**生产实例化**（`ClassName(` 真的出现在生产代码里）。

口径（严格定义，避免假阳性）
--------------------------
「生产」= 满足**全部**下列条件的 `.py` 文件：
* 不在 `tests/` `tools/` `legacy/` `测试/` `build*` `dist*` `文档/` `node_modules/` `.venv/`；
* 文件名**不以 `_example.py` 结尾**（示例不算生产）；
* 调用点**不在模块级 `if __name__ == "__main__":` 块内**（`__main__` 是手动运行入口）。

⚠️ 这个口径**已知的假阴性**（故意留白，不要假装它全能）：
「类被当值传递、再由别处 `cls()` 实例化」抓不到（如 `GATEWAY_TYPES = {'modbus': ModbusGateway}`
→ `gateway_class(config)`）。所以本守卫**只对登记表里的模块生效**，
不宣称「全仓零实例化模块只有这些」。要做全量判定得建生产调用图，那是另一件事。
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

SKIP_DIRS = {"tests", "tools", "legacy", "测试", "build", "dist",
             "node_modules", "文档", ".venv"}

#: 平行实现登记表：模块相对路径 → 理由（**必须写清「生产实际用的是哪一份」**）。
#:
#: 登记 ≠ 放行。它的作用是：**把事实露出来**，并在有人真的接上它时立刻变红。
PARALLEL_IMPLS: dict[str, str] = {
    "timeseries/mqtt_to_tsdb.py": (
        "MQTT→TDengine 写入服务（4 个公开类）。生产实际走 "
        "智能层/tsdb_adapter.py::RealtimeDataBridge。仅被 timeseries/integration_example.py 使用。"
        "待主人裁决：接线 / 删除 / 明确保留为示例。"
    ),
    "gateway/mqtt_subscriber.py": (
        "MQTT 订阅器 + 数据分发器。生产从未实例化 —— gateway/run_gateway.py 只建 "
        "Modbus/S7/OPCUA/IEC104/DNP3 网关，不含 MQTT 订阅；gateway/__init__.py 只是 re-export。"
        "待主人裁决：接线 / 删除 / 明确保留为示例。"
    ),
    "存储层/data_archive.py": (
        "数据归档（573 行）。生产归档由 core/maintenance.py 的 data_archive 任务调用 "
        "存储层/database.py::archive_old_data 完成，**不经 DataArchive**。"
        "两个实现的存在会互相误导（database.py 的注释还引用了 data_archive.archive_data）。"
        "待主人裁决：接线 / 删除 / 明确保留。"
    ),
    "timeseries/query_builder.py": (
        "TDengine 查询构建器（5 个公开类）。仅被 timeseries/integration_example.py 与自身 "
        "docstring 使用，生产零实例化。待主人裁决：接线 / 删除 / 明确保留为示例。"
    ),
    "timeseries/migration.py": (
        "SQLite→TDengine 迁移器（533 行）。只在自身 `__main__` 里实例化（手动运行），"
        "生产链路零调用。待主人裁决：接线 / 删除 / 明确保留为运维工具。"
    ),
}


# ---------------------------------------------------------------------------
# 口径实现
# ---------------------------------------------------------------------------

def _production_files(root: pathlib.Path) -> list[pathlib.Path]:
    out = []
    for p in root.rglob("*.py"):
        parts = str(p.relative_to(root)).replace("\\", "/").split("/")
        if any(x.startswith(".") for x in parts[:-1]):
            continue
        if parts[0] in SKIP_DIRS or parts[0].startswith(("dist", "build")):
            continue
        if p.name.endswith("_example.py"):
            continue
        out.append(p)
    return out


def _main_block_calls(tree: ast.Module) -> set[int]:
    """模块级 `if __name__ == '__main__':` 块内所有节点的 id（要排除的调用点）。"""
    ids: set[int] = set()
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"):
            continue
        if not any(isinstance(c, ast.Constant) and c.value == "__main__"
                   for c in test.comparators):
            continue
        for sub in ast.walk(node):
            ids.add(id(sub))
    return ids


def _called_names(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set()
    skip = _main_block_calls(tree)
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in skip:
            continue
        f = node.func
        if isinstance(f, ast.Name):
            names.add(f.id)
        elif isinstance(f, ast.Attribute):
            names.add(f.attr)
    return names


def production_instantiated(root: pathlib.Path) -> set[str]:
    """全仓「生产里出现过 `X(...)` 调用」的名字集合。"""
    out: set[str] = set()
    for p in _production_files(root):
        out |= _called_names(p)
    return out


def public_classes(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set()
    return {n.name for n in tree.body
            if isinstance(n, ast.ClassDef) and not n.name.startswith("_")}


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------

def test_registry_entries_exist_and_have_classes():
    """登记表不得指向不存在的模块 / 已改名的类。"""
    bad = []
    for rel, reason in PARALLEL_IMPLS.items():
        p = ROOT / rel
        if not p.exists():
            bad.append(f"{rel}: 文件不存在")
            continue
        if not public_classes(p):
            bad.append(f"{rel}: 没有公开类 —— 登记口径不适用了")
        if not reason.strip():
            bad.append(f"{rel}: 没写理由")
    assert not bad, "PARALLEL_IMPLS 需要清理：\n" + "\n".join(f"  {b}" for b in bad)


def test_registry_entries_are_really_zero_instantiated():
    """⚠️ 反向守卫：登记为「生产零实例化」的模块，一旦被接上就必须从表里删掉。

    这条同时是**登记表正确性**的守卫 —— 它逼着每条登记都经得起机械复核。
    """
    sites = production_instantiated(ROOT)
    offenders = []
    for rel in PARALLEL_IMPLS:
        alive = sorted(c for c in public_classes(ROOT / rel) if c in sites)
        if alive:
            offenders.append(
                f"{rel}: 这些公开类已在**生产**代码里被实例化 {alive} —— "
                f"请从 PARALLEL_IMPLS 删除该条，并同步更新 README/DEPLOYMENT"
            )
    assert not offenders, "\n".join(offenders)


def test_scan_has_discriminative_power(tmp_path):
    """前置对照（不依赖本仓库）：证明「生产实例化」口径真的能区分两种情况。

    若把 `__main__` 块内的实例化也算进来，或把示例文件算成生产，
    本守卫就会**全绿空转**（那正是要防的）。
    """
    (tmp_path / "prod.py").write_text(
        "from a import A\n\n\ndef build():\n    return A()\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    # 只在 __main__ 里实例化 → 不算生产
    (tmp_path / "b.py").write_text(
        "class B:\n    pass\n\n\nif __name__ == '__main__':\n    B()\n", encoding="utf-8")
    # 只在示例文件里实例化 → 不算生产
    (tmp_path / "c.py").write_text("class C:\n    pass\n", encoding="utf-8")
    (tmp_path / "c_example.py").write_text("from c import C\nC()\n", encoding="utf-8")

    sites = production_instantiated(tmp_path)

    assert "A" in sites, "生产里的 A() 没被扫到 —— 口径坏了"
    assert "B" not in sites, "__main__ 块内的实例化被误判成生产 —— 守卫会全绿空转"
    assert "C" not in sites, "示例文件里的实例化被误判成生产 —— 守卫会全绿空转"


def test_registry_is_not_empty():
    """元守卫：表被清空要么是「全部接线/删除」的有意决定，要么是口径退化。

    两种情况都必须**有意识地**处理，不能让守卫静默失效。
    """
    assert PARALLEL_IMPLS, (
        "PARALLEL_IMPLS 空了 —— 若确实是「所有平行实现都已接线或删除」，"
        "请删掉本文件并说明；否则说明口径退化了"
    )
