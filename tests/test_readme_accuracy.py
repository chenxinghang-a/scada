# -*- coding: utf-8 -*-
"""README **准确性**守卫：文档里写的东西必须真的存在。

缺陷类别：**文档声称的东西会腐烂**
----------------------------------
这是同一类问题的第 4 次（前三次都在代码注释里）：

| 轮次 | 形态 |
|---|---|
| 197 | **注释声称的接线**其实不存在（`App.vue` 没有 `onErrorCaptured`） |
| 198 | **文档里写了要做**却没人做（语言包键齐全的单测） |
| 199 | **注释里的地图**已与实际不符（漏记一份 + 行号漂移） |
| **202** | **README（最显眼的文档）里的目录树与版本号已腐烂** |

实测（2026-10-09，round 202）：

* README 标题写 **`v1.3.1028`**，而 `VERSION` 是 **`1.3.1072`** —— **落后 44 个版本**；
* README 的目录树列了 **79 个文件，其中 6 个根本不存在**：
  `采集层/device_manager_factory.py`、`采集层/s7_client.py`、
  `报警层/alarm_statistics.py`、`报警层/interfaces.py`、
  `用户层/audit_logger.py`、`timeseries/offline_buffer.py`。

README 是**答辩老师最先读的东西** —— 树里写了一个不存在的文件，
就是一个现成的追问点。本守卫把它变成机械可查的。

口径
----
* 只校验目录树里**带扩展名的文件行**（目录行不校验存在性 —— 空目录会被 git 丢掉）；
* 只校验**双向里的一向**：树里列了但不存在 → 红。
  「存在但树里没列」**不查** —— README 的树本就是节选，不要求穷举；
* 版本号：README 标题里的 `vX.Y.Z` 必须等于 `VERSION` 文件。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
README = BACKEND_ROOT / "README.md"
VERSION_FILE = BACKEND_ROOT / "VERSION"

#: 目录树里**允许**列但文件不存在的条目（登记时必须写清理由）。
#:
#: 空表 = 当前树里列的每个文件都真实存在。
ALLOWED_MISSING: dict[str, str] = {}

_CHECK_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".txt", ".spec", ".bat", ".json"}


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _tree_block() -> str:
    """取 README 里 `industrial_scada/` 开头那个目录树代码块。"""
    m = re.search(r"```\n(industrial_scada/[\s\S]*?)```", _readme())
    assert m, "README 里没找到以 `industrial_scada/` 开头的目录树代码块 —— 口径可能坏了"
    return m.group(1)


def _tree_files() -> list[str]:
    """从目录树里解析出**文件**的相对路径（目录行不算）。"""
    stack: list[tuple[int, str]] = []
    out: list[str] = []
    for line in _tree_block().splitlines():
        name = re.sub(r"^[│├└─\s]+", "", line).split("#")[0].strip()
        if not name or name.startswith("industrial_scada"):
            continue
        indent = len(line) - len(line.lstrip("│ ├└─"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if name.endswith("/"):
            stack.append((indent, name.rstrip("/")))
            continue
        parts = [s[1] for s in stack] + [name]
        p = Path("/".join(parts))
        if p.suffix in _CHECK_SUFFIXES:
            out.append(str(p).replace("\\", "/"))
    return out


# ---------------------------------------------------------------------------
# 1. 目录树里列的文件必须存在
# ---------------------------------------------------------------------------


def test_tree_parsing_is_effective():
    """元守卫：解析出来的文件太少时，下面的断言会退化成空断言。"""
    files = _tree_files()
    assert len(files) > 50, f"只从目录树解析出 {len(files)} 个文件 —— 口径可能坏了"


def test_every_file_in_readme_tree_exists():
    """README 目录树里列出的每个文件都必须真实存在。"""
    missing = [
        f for f in _tree_files()
        if not (BACKEND_ROOT / f).exists() and f not in ALLOWED_MISSING
    ]
    assert not missing, (
        "README 的目录树里列了以下文件，但它们**不存在** ——\n"
        "README 是答辩时最先被读的文档，树里写不存在的文件就是一个现成的追问点。\n"
        "请改成真实路径，或删掉该行：\n  " + "\n  ".join(missing)
    )


def test_allowlist_is_not_stale():
    """ALLOWED_MISSING 里的条目如果已经存在了，就该删掉。"""
    stale = [f for f in ALLOWED_MISSING if (BACKEND_ROOT / f).exists()]
    assert not stale, f"以下文件已存在，请从 ALLOWED_MISSING 删掉：{stale}"


# ---------------------------------------------------------------------------
# 2. README 标题里的版本号必须等于 VERSION
# ---------------------------------------------------------------------------


def test_readme_version_matches_version_file():
    """README 标题里的 `vX.Y.Z` 必须等于 `VERSION`。

    实测：README 曾写 `v1.3.1028` 而 `VERSION` 是 `1.3.1072`（落后 44 个版本）。
    这类漂移没有守卫时**必然发生** —— 因为发布流程只改 `VERSION`，不会想起 README。
    """
    m = re.search(r"^#\s.*?\bv(\d+\.\d+\.\d+)\b", _readme(), re.M)
    assert m, "README 标题里没找到形如 vX.Y.Z 的版本号 —— 口径可能坏了"
    readme_ver = m.group(1)
    real_ver = VERSION_FILE.read_text(encoding="utf-8").strip()
    assert readme_ver == real_ver, (
        f"README 标题写 v{readme_ver}，而 VERSION 是 {real_ver} —— 文档版本已漂移。\n"
        f"请把 README 标题改成 v{real_ver}。"
    )
