# -*- coding: utf-8 -*-
"""代码**指针**守卫：注释/文档里写的 `路径/文件.py:行号` 必须指向**存在**的文件。

缺陷类别：**指针会指向已删除的东西，而没人发现**
------------------------------------------------
这是「散文承诺会腐烂」的第 5 次（197 注释声称的接线 / 198 文档写了没人做 /
199 注释里的地图 / 202 README / **203 注释里的代码指针**）。

实测（2026-10-09，round 203）：生产 py 里有 **30 个** `文件.py:行号` 形式的指针，
其中 **4 个指向已被 D4 死代码清理删掉的文件** —— 全在 `core/index_bootstrap.py`：

    core/index_bootstrap.py  'CREATE INDEX idx_alarm_timestamp …' 的【为谁建】注释里
      → core/report_generator.py:267 / :274 / :297   ← 该文件已按 D4 删除
      → 报警层/alarm_kpi.py:133                      ← 该文件已按 D4 删除

**后果**：那 2 个索引的「存在理由」现在**指向不存在的代码** ——
将来没人能验证它们该不该保留（而 `index_bootstrap.py` 的整个设计
就是「每条索引都写明为谁建」）。

顺带（同一轮发现）：`docs/final_summary.md` 里可复制的 curl
`/api/alarms/kpi` 与 `/api/alarms/kpi/export` **后端没有实现**
（那个功能模块 `报警层/alarm_kpi.py` 也按 D4 删了）—— 照着跑会 404。

口径
----
* 只扫**生产 py**（core/展示层/报警层/采集层/存储层/智能层/用户层/timeseries/gateway/tools
  + 根目录几个入口文件）；
* 只认 `<…>.py:<数字>` 这种**带行号**的写法（那是明确的"去看这里"）；
* 文件是否存在：在「仓库根 / 引用者同目录 / 几个常见子目录」里找，
  找到任一个即算存在；
* 确有理由引用**已删除**文件的，进 `ALLOWED_MISSING` 并写明理由（双向断言）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = [
    "core", "展示层", "报警层", "采集层", "存储层", "智能层", "用户层",
    "timeseries", "gateway", "tools",
]
SCAN_FILES = ["run.py", "launcher.py", "config.py", "paths.py"]

#: 指针可能相对的几个根（按顺序尝试）。
RESOLVE_ROOTS = [".", "采集层", "core", "报警层", "展示层", "存储层", "智能层", "timeseries"]

#: `路径/文件.py:123`
_POINTER_RE = re.compile(r"([\w\u4e00-\u9fff/\\-]+\.py):(\d+)")

#: 确有理由指向**已不存在**文件的指针（登记时必须写清理由）。
#:
#: 空表 = 当前所有指针都指向存在的文件。
ALLOWED_MISSING: dict[str, str] = {}


def _iter_sources() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        root = BACKEND_ROOT / d
        if root.is_dir():
            out += [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]
    for n in SCAN_FILES:
        p = BACKEND_ROOT / n
        if p.is_file():
            out.append(p)
    return out


def _resolves(pointer: str, from_file: Path) -> bool:
    """指针是否能解析到一个**存在**的文件。"""
    cands = [BACKEND_ROOT / pointer, from_file.parent / pointer]
    cands += [BACKEND_ROOT / r / pointer for r in RESOLVE_ROOTS]
    return any(c.is_file() for c in cands)


def _scan() -> tuple[int, dict[str, list[str]]]:
    total = 0
    missing: dict[str, list[str]] = {}
    for f in _iter_sources():
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _POINTER_RE.finditer(text):
            total += 1
            pointer = m.group(1)
            if _resolves(pointer, f):
                continue
            rel = f.relative_to(BACKEND_ROOT).as_posix()
            missing.setdefault(pointer, []).append(f"{rel}:{m.group(2)}")
    return total, missing


def test_scan_is_effective():
    """元守卫：扫不到指针时，下面的断言会退化成空断言。"""
    total, _ = _scan()
    assert total > 10, f"只扫到 {total} 个代码指针 —— 口径可能坏了"


def test_all_code_pointers_resolve():
    """注释/文档里写的 `文件.py:行号` 必须指向存在的文件。"""
    _, missing = _scan()
    bad = {
        p: refs for p, refs in missing.items()
        if p not in ALLOWED_MISSING
    }
    assert not bad, (
        "以下代码指针指向**不存在**的文件 —— 指针是「去看这里」的意思，\n"
        "指向空处会让注释失去可验证性（D4 删模块后尤其容易发生）：\n"
        + "\n".join(
            f"  {p}  ← " + "、".join(refs[:4]) + (f"（共 {len(refs)} 处）" if len(refs) > 4 else "")
            for p, refs in sorted(bad.items())
        )
        + "\n\n请把指针改成真实位置，或删掉该引用；"
        "确有理由指向已删除文件的，登记进 ALLOWED_MISSING 并写明理由。"
    )


def test_allowlist_is_not_stale():
    """ALLOWED_MISSING 里的指针如果已经能解析了，就该删掉。"""
    stale = [
        p for p in ALLOWED_MISSING
        if _resolves(p, BACKEND_ROOT)
    ]
    assert not stale, f"以下指针已能解析，请从 ALLOWED_MISSING 删掉：{stale}"
