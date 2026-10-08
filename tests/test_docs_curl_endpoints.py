# -*- coding: utf-8 -*-
"""文档里**可复制的 curl 端点**必须真实存在。

缺陷类别：**给人复制运行的命令指向 404**
----------------------------------------
`docs/` 是给人看的，里面的 `curl …` 是**给人复制运行的** ——
指向一个不存在的端点，读者会拿到 404，然后怀疑系统坏了。

实测（2026-10-09，round 203）：

    docs/final_summary.md:
        curl http://localhost:5000/api/alarms/kpi?hours=24
        curl http://localhost:5000/api/alarms/kpi/export?hours=24&format=text

**后端没有 `/api/alarms/kpi`** —— 承载它的 `报警层/alarm_kpi.py`
已按 D4 死代码清理（`07a6e9c`）删除（它**生产不可达**，从未接线）。
文档却仍把它写成"已实现的功能"。

口径
----
* 只抽 **`curl` 命令里**的 `/api/...` URL（不是正文里随便提到的路径 ——
  正文里常把**具体实例**写出来，例如 `/api/health/modules/device_control`
  对应的是参数化路由 `/api/health/modules/<module_name>`，那是**合法**的）；
* 匹配时把文档里的**具体段**与后端的**参数段**互相归一（见 `_structural`）；
* 确实要写一个**不存在**的端点的（例如"历史遗留"章节），进 `ALLOWED_MISSING`
  并写明理由（双向断言）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = BACKEND_ROOT / "docs"

#: `curl [选项] <url>` —— 只认 curl 命令里的 URL
_CURL_RE = re.compile(r"curl\s+(?:-[A-Za-z]+\s+)*['\"]?(https?://[^\s'\"]+|/[^\s'\"]+)")

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
