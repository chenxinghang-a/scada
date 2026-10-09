# -*- coding: utf-8 -*-
"""文档里**可复制的命令**必须真的能跑：端点存在、引用的文件存在。

缺陷类别：**给人复制运行的命令指向 404 / No such file**
-------------------------------------------------------
`docs/` 是给人看的，里面的命令是**给人复制运行的** ——
指向不存在的端点或文件，读者会拿到 404 / `No such file or directory`，
然后怀疑系统坏了。

实测（2026-10-09）：

* round 203 —— `docs/final_summary.md` 里可复制的 curl 指向**不存在的端点**：

      curl http://localhost:5000/api/alarms/kpi?hours=24
      curl http://localhost:5000/api/alarms/kpi/export?hours=24&format=text

  后端没有 `/api/alarms/kpi` —— 承载它的 `报警层/alarm_kpi.py`
  已按 D4 死代码清理（`07a6e9c`）删除（它**生产不可达**，从未接线）。

* round 204 —— `docs/ICS_SECURITY.md` 的「自动化脚本」段给了 **3 个从未存在过**的脚本：

      python tools/security_check.py       ← 无 git 历史，从未存在
      python tools/vulnerability_scan.py   ← 同上
      python tools/compliance_check.py     ← 同上

  真身是 `tools/security_scan.py`（`full` / `quick` / `report`）。

口径
----
* 端点：只抽 **`curl` 命令里**的 `/api/...` URL（不是正文里随便提到的路径 ——
  正文里常把**具体实例**写出来，例如 `/api/health/modules/device_control`
  对应的是参数化路由 `/api/health/modules/<module_name>`，那是**合法**的）；
  匹配时把文档里的**具体段**与后端的**参数段**互相归一（见 `_structural`）；
* 文件：只抽 **命令动词后面**的项目文件（`python xxx.py` / `bash xxx.sh` /
  `node xxx.js` / `npm run xxx`），不抽正文里提到的文件名（那可能是在讲历史）；
* 确实要写一个**不存在**的端点/文件的（例如"历史遗留"章节），进
  `ALLOWED_MISSING` 并写明理由（双向断言）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = BACKEND_ROOT / "docs"

#: `curl [选项] <url>` —— 只认 curl 命令里的 URL
_CURL_RE = re.compile(r"curl\s+(?:-[A-Za-z]+\s+)*['\"]?(https?://[^\s'\"]+|/[^\s'\"]+)")

#: `python|bash|node … <项目文件>` —— 只认命令动词后面的文件参数
_CMD_FILE_RE = re.compile(
    r"\b(?:python3?|py|bash|sh|node)\s+['\"]?"
    r"([\w\u4e00-\u9fff./\\-]+\.(?:py|sh|js|mjs|cjs|bat|ps1))\b"
)

#: 已确认**故意**指向不存在端点的（登记时必须写清理由）。
ALLOWED_MISSING: dict[str, str] = {}


def _backend_routes() -> set[tuple[str, str]]:
    """复用契约测试的后端路由解析（单一真源）。"""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "_tfc_for_docs", BACKEND_ROOT / "tests" / "test_frontend_contract.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_tfc_for_docs"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:  # 该模块可能在 import 期做断言
        pass

    routes: set[tuple[str, str]] = set()
    for f in mod._iter_backend_source_files():
        for r in mod._parse_backend_file(f):
            routes.add((r.method, r.path))
    return routes


def _structural(path: str) -> tuple[str, ...]:
    """把路径归一成结构：参数段统一成 `*`（`<name>` / `{name}` / 具体值都算）。"""
    out = []
    for seg in path.strip("/").split("/"):
        if seg.startswith("<") or seg.startswith("{") or seg.startswith(":"):
            out.append("*")
        else:
            out.append(seg)
    return tuple(out)


def _doc_curl_urls() -> dict[str, set[str]]:
    """`docs/*.md` 里 curl 命令中的 /api 端点（按文件归并）。"""
    out: dict[str, set[str]] = {}
    for f in sorted(DOCS_DIR.glob("*.md")):
        for m in _CURL_RE.finditer(f.read_text(encoding="utf-8", errors="replace")):
            url = m.group(1)
            p = re.sub(r"^https?://[^/]+", "", url).split("?")[0].rstrip("/")
            if p.startswith("/api"):
                out.setdefault(f.name, set()).add(p)
    return out


def test_docs_curl_scan_is_effective():
    """元守卫：一条 curl 都扫不到时，下面的断言会退化成空断言。"""
    urls = _doc_curl_urls()
    total = sum(len(v) for v in urls.values())
    assert total > 5, f"只从 docs 里扫到 {total} 条 curl 端点 —— 口径可能坏了"


def test_every_doc_curl_endpoint_exists():
    """文档里 curl 的每个端点都必须能在后端找到。"""
    routes = _backend_routes()
    assert routes, "后端路由一条都没解析到 —— 口径可能坏了"
    route_structs = {_structural(p) for _, p in routes}

    bad: list[str] = []
    for fname, urls in sorted(_doc_curl_urls().items()):
        for p in sorted(urls):
            if p in ALLOWED_MISSING:
                continue
            # 具体实例 ↔ 参数段互相归一：/a/b/c 与 /a/<x> 结构相同即算命中
            norm = _structural(p)
            hit = norm in route_structs
            if not hit:
                # 再试：把文档路径的**末段当具体实例**、后端有更长的参数化路由
                for rs in route_structs:
                    if len(rs) == len(norm) and all(
                        a == b or a == "*" or b == "*" for a, b in zip(rs, norm)
                    ):
                        hit = True
                        break
            if not hit:
                bad.append(f"  {fname}: {p}")
    assert not bad, (
        "以下 `docs/` 里可复制的 curl 端点在后端**不存在** —— 读者会拿到 404：\n"
        + "\n".join(bad)
        + "\n\n请改成真实端点；确有理由保留的，登记进 ALLOWED_MISSING 并写明理由。"
    )


def test_allowlist_is_not_stale():
    """ALLOWED_MISSING 里登记后又被补上的端点，应删掉登记。"""
    routes = _backend_routes()
    route_structs = {_structural(p) for _, p in routes}
    stale = [p for p in ALLOWED_MISSING if _structural(p) in route_structs]
    assert not stale, f"以下端点已存在，请从 ALLOWED_MISSING 删掉：{stale}"


# ---------------------------------------------------------------------------
# 第二部分：文档里可复制的命令引用的**项目文件**必须存在
# ---------------------------------------------------------------------------

#: 已确认**故意**引用不存在文件的（登记时必须写清理由）。
ALLOWED_MISSING_FILES: dict[str, str] = {}


def _doc_cmd_files() -> dict[str, set[str]]:
    """`docs/*.md`（含根 README / 启动说明）里命令动词后面的项目文件。"""
    out: dict[str, set[str]] = {}
    targets = list(DOCS_DIR.glob("*.md")) + [
        BACKEND_ROOT / "README.md",
        BACKEND_ROOT / "启动说明.md",
    ]
    for f in targets:
        if not f.is_file():
            continue
        for m in _CMD_FILE_RE.finditer(f.read_text(encoding="utf-8", errors="replace")):
            rel = m.group(1).replace("\\", "/").lstrip("./")
            out.setdefault(f.name, set()).add(rel)
    return out


def test_doc_cmd_file_scan_is_effective():
    """元守卫：一条都扫不到时，下面的断言会退化成空断言。"""
    files = _doc_cmd_files()
    total = sum(len(v) for v in files.values())
    assert total > 3, f"只从文档里扫到 {total} 个命令引用的文件 —— 口径可能坏了"


def test_every_doc_cmd_file_exists():
    """文档里 `python xxx.py` 这类命令引用的文件必须真实存在。"""
    bad: list[str] = []
    for fname, files in sorted(_doc_cmd_files().items()):
        for p in sorted(files):
            if p in ALLOWED_MISSING_FILES:
                continue
            if not (BACKEND_ROOT / p).is_file():
                bad.append(f"  {fname}: {p}")
    assert not bad, (
        "以下 `docs/` 里可复制的命令引用的文件**不存在** —— "
        "读者会拿到 `No such file or directory`：\n"
        + "\n".join(bad)
        + "\n\n请改成真实文件（或真实的命令动词）；"
        "确有理由保留的，登记进 ALLOWED_MISSING_FILES 并写明理由。"
    )


def test_missing_file_allowlist_is_not_stale():
    """登记后文件又被补上的，应删掉登记。"""
    stale = [p for p in ALLOWED_MISSING_FILES if (BACKEND_ROOT / p).is_file()]
    assert not stale, f"以下文件已存在，请从 ALLOWED_MISSING_FILES 删掉：{stale}"


# ---------------------------------------------------------------------------
# 第三部分：文档里**带目录的 `.py` 引用**必须存在
# ---------------------------------------------------------------------------

#: `目录/文件.py`（至少一个 `/`）—— 裸文件名不查：同名文件可能在不同目录，
#: 裸名匹配会假阳性（实测：README 里的 `query_builder.py` 指的是
#: `timeseries/query_builder.py`（存在），而 `core/query_builder.py` 已被 D4 删除）。
_DOC_PY_PATH_RE = re.compile(
    r"([\w\u4e00-\u9fff][\w\u4e00-\u9fff-]*(?:/[\w\u4e00-\u9fff-]+)+\.py)\b"
)

#: 已确认**故意**引用不存在文件的（都是「历史说明」段：写明某模块已删除 / 从未存在）。
ALLOWED_MISSING_PY_PATHS: dict[str, str] = {
    "tools/security_check.py":
        "docs/ICS_SECURITY.md 的修复说明段：写明这三个脚本名**从未存在**，"
        "故保留原名以便读者对照。",
    "tools/vulnerability_scan.py": "同上（从未存在）。",
    "tools/compliance_check.py": "同上（从未存在）。",
    "报警层/alarm_kpi.py":
        "docs/final_summary.md 的修复说明段：写明该模块**已按 D4 删除**、"
        "且从未接线。",
    "采集层/device_manager_factory.py":
        "docs/architecture_improvements.md 的修复说明段：写明该文件**已按 D4 删除**。",
}


def _doc_py_paths() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    targets = list(DOCS_DIR.glob("*.md")) + [
        BACKEND_ROOT / "README.md",
        BACKEND_ROOT / "启动说明.md",
    ]
    for f in targets:
        if not f.is_file():
            continue
        for m in _DOC_PY_PATH_RE.finditer(f.read_text(encoding="utf-8", errors="replace")):
            out.setdefault(f.name, set()).add(m.group(1).replace("\\", "/").lstrip("./"))
    return out


def test_doc_py_path_scan_is_effective():
    """元守卫：一条都扫不到时，下面的断言会退化成空断言。"""
    paths = _doc_py_paths()
    total = sum(len(v) for v in paths.values())
    assert total > 10, f"只从文档里扫到 {total} 个带目录的 .py 引用 —— 口径可能坏了"


def test_every_doc_py_path_exists():
    """文档里带目录的 `.py` 引用必须真实存在。

    D4 删了 69 个生产不可达模块 —— 文档里对它们的**声明式**引用会随之失效
    （实测：`docs/architecture_improvements.md` 的「新增文件」清单里
    列着 `采集层/device_manager_factory.py`，而它已按 D4 删除）。
    """
    bad: list[str] = []
    for fname, paths in sorted(_doc_py_paths().items()):
        for p in sorted(paths):
            if p in ALLOWED_MISSING_PY_PATHS:
                continue
            cands = [BACKEND_ROOT / p]
            cands += [BACKEND_ROOT / d / p for d in
                      ("采集层", "core", "展示层", "报警层", "存储层", "智能层",
                       "用户层", "timeseries", "gateway", "tools", "tests")]
            if not any(c.is_file() for c in cands):
                bad.append(f"  {fname}: {p}")
    assert not bad, (
        "以下 `docs/` 里带目录的 `.py` 引用**不存在** —— "
        "文档在描述一个已被删除（或从未存在）的文件：\n"
        + "\n".join(bad)
        + "\n\n请改成真实路径；确有理由保留的（例如「历史说明」段），"
        "登记进 ALLOWED_MISSING_PY_PATHS 并写明理由。"
    )


def test_missing_py_path_allowlist_is_not_stale():
    """登记后文件又被补上的，应删掉登记。"""
    stale = []
    for p in ALLOWED_MISSING_PY_PATHS:
        cands = [BACKEND_ROOT / p]
        cands += [BACKEND_ROOT / d / p for d in
                  ("采集层", "core", "展示层", "报警层", "存储层", "智能层",
                   "用户层", "timeseries", "gateway", "tools", "tests")]
        if any(c.is_file() for c in cands):
            stale.append(p)
    assert not stale, f"以下文件已存在，请从 ALLOWED_MISSING_PY_PATHS 删掉：{stale}"
