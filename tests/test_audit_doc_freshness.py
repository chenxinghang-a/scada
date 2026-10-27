# -*- coding: utf-8 -*-
"""审计文档不许悄悄落后于代码（round 297）。

为什么需要这条守卫
------------------
`SCADA-深度审计-*.md` 是交接与答辩时唯一的"我们做过什么"的凭证。
它曾经落后过整整一轮交接（278–282 根本没成节，只留了一句
"未在本文件单独成节，见 git log"）—— 而 `git log` 只有当事人在场时才看。

"记得更新文档"是纪律，纪律会失效；**闸门不会**。
这里用代码里真实存在的 `# round NNN：` 注释作为事实来源（不依赖 git，
CI 浅克隆也能跑），要求文档表格里的最大轮次不能落后太多。

阈值
----
允许滞后 `MAX_LAG = 2` 轮：有些轮次只是小修（或纯测试），不必每次都动文档。
但落后 3 轮以上就会红 —— 那已经不是"顺手"，是忘了。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOC_GLOB = 'SCADA-深度审计-*.md'

#: 允许文档落后代码多少轮
MAX_LAG = 2

#: 源码里 `# round 283：...` 这种注释（只认注释，避免 `round(1.5)` 之类）
_CODE_ROUND_RE = re.compile(r'#\s*round\s+(\d{3})')

#: 文档表格里 `| 283 | ...` 这种轮次行
_DOC_ROUND_RE = re.compile(r'^\|\s*(\d{3})\s*\|', re.MULTILINE)


def _in_foreign_repo(path: Path) -> bool:
    """该文件是否落在**兄弟仓库**里（`REPO/xxx/.git` 存在）。

    ⚠️ 这里踩过一次坑：最初写成
    `if (path.parent / '.git').exists() or (path.parent.parent / '.git').exists()`，
    对本仓 `gateway/base_gateway.py` 来说 `path.parent.parent` 就是 **REPO 自己**
    —— 它有 `.git`，于是**本仓所有二级目录下的源码全被跳过**。
    守卫因此只扫到 round 284，而文档已经到 292 → 判"够新" → **假绿**。
    （wb 提醒过"兄弟仓库要跳过"，我跳过头了。）

    正确口径：只看**相对 REPO 的第一层目录**是不是独立仓库。
    """
    try:
        rel = path.relative_to(REPO)
    except ValueError:
        return True
    if len(rel.parts) < 2:
        return False
    return (REPO / rel.parts[0] / '.git').exists()


def _iter_source_files():
    for pattern in ('*.py', '*/*.py', '*/*/*.py'):
        for path in REPO.glob(pattern):
            if 'tests' in path.parts or '.venv' in path.parts:
                continue
            if _in_foreign_repo(path):
                continue
            yield path


def max_code_round() -> int:
    """源码注释里出现过的最大轮次号。"""
    found = []
    for path in _iter_source_files():
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        found.extend(int(m) for m in _CODE_ROUND_RE.findall(text))
    return max(found) if found else 0


def doc_path() -> Path:
    docs = sorted(REPO.glob(DOC_GLOB))
    assert docs, f'找不到审计文档 {DOC_GLOB}'
    return docs[-1]


def max_doc_round(text: str) -> int:
    """文档轮次表格里的最大轮次号。"""
    found = [int(m) for m in _DOC_ROUND_RE.findall(text)]
    return max(found) if found else 0


# ---------------------------------------------------------------------------
# 真守卫
# ---------------------------------------------------------------------------

def test_audit_doc_is_not_far_behind_code():
    code_max = max_code_round()
    assert code_max > 0, '前提不成立：源码里一个 `# round NNN` 注释都没扫到'

    doc_max = max_doc_round(doc_path().read_text(encoding='utf-8', errors='ignore'))

    assert doc_max >= code_max - MAX_LAG, (
        f'审计文档落后了：代码已到 round {code_max}，文档表格最大只到 '
        f'round {doc_max}（允许滞后 {MAX_LAG} 轮）。\n'
        f'请把新轮次补进 `{doc_path().name}` 的轮次表 —— '
        f'这份文档是交接与答辩时唯一的"做过什么"的凭证。'
    )


def test_doc_has_round_table():
    """反向对照：文档里必须真的有一张轮次表（否则上面那条恒绿）。"""
    text = doc_path().read_text(encoding='utf-8', errors='ignore')
    assert max_doc_round(text) > 0, (
        '文档里扫不到任何 `| NNN |` 轮次行 —— 守卫会恒绿，等于没有')


# ---------------------------------------------------------------------------
# 反向对照：比较逻辑本身要能报错
# ---------------------------------------------------------------------------

def test_lag_check_catches_stale_doc():
    """落后超过阈值必须判红（用伪造数据，证明断言不是恒真）。"""
    code_max, doc_max = 300, 290
    assert not (doc_max >= code_max - MAX_LAG), (
        '落后 10 轮却被判为"够新" —— 守卫逻辑写错了')


def test_lag_check_accepts_recent_doc():
    """刚好在阈值内必须放行（防假阳性）。"""
    code_max, doc_max = 300, 300 - MAX_LAG
    assert doc_max >= code_max - MAX_LAG
