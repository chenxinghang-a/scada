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

分析与口径：见 `tools/wiring_analysis.py`（**单一真源**，同一套分析也被
`tools/report_dead_modules.py` 用来出「死模块清单」）。

`tools/`（开发脚本）与 `tests/` 不参与：它们本就不该被 `run.py` 引用。

⚠️ 静态可达性分析**天然有假阳性**（动态 import、字符串拼模块名、插件注册表）。
所以留了 `ALLOWLIST`：确有"静态看不见的接线方式"的模块登记在此，**并写明理由**。
新增条目必须先解释清楚 —— 这正是本守卫想逼出来的那个"有意识的决定"。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from wiring_analysis import MARKER_RE, analyse  # noqa: E402

# 静态分析看不见接线方式的模块（登记时必须写理由）。
ALLOWLIST: dict[str, str] = {}

# 「**可达**却仍标着未接线」的已知历史遗留。
# 这类通常意味着：模块能被 import（例如被某个包的 __init__ 顺带导入），
# 但能力并未被真正调用 —— 声明的**措辞**与事实不符，需要逐个核实后改口径。
# 登记在此是为了**把事实露出来**，而不是用 skip 藏起来。
REACHABLE_BUT_MARKED_KNOWN = {
    "core.generate_certs": "被 core/__init__ 间接导入；是否有调用方待核实",
}

REPORT = analyse(ROOT)
PATH_OF = REPORT.path_of
UNREACHABLE = REPORT.unreachable


def test_analysis_is_sane():
    """守卫自身的前置对照：分析必须真的在跑，且结论不是"全都可达/全都不可达"。"""
    assert len(PATH_OF) > 100, f"只扫到 {len(PATH_OF)} 个模块，分析口径可能坏了"
    assert "run" in PATH_OF, "入口 run.py 没被扫到"
    assert UNREACHABLE, "一个不可达模块都没有 —— 分析口径可能坏了（假绿）"
    assert len(UNREACHABLE) < len(PATH_OF), "全部不可达 —— 分析口径可能坏了"


def test_analysis_ignores_other_repos():
    """**扫描范围必须只含本仓库的产品代码。**

    踩过（2026-10-08）：CI 的 test job 会把前端仓库 checkout 到 `<workspace>/scada-app`，
    于是 `rglob("*.py")` 扫到了 `scada-app/backend/build_entry.py` → 被判"未声明"
    → **CI 红、本机绿**（本机没有那个 checkout）。这条断言把那类越界挡在门口。
    """
    intruders = sorted(m for m in PATH_OF if m.startswith("scada-app"))
    assert not intruders, f"扫到了别的仓库的文件：{intruders}"
    # 反向对照：本仓库自己的包必须在
    for must in ("run", "采集层.simulated_device_manager", "展示层.api.api_data"):
        assert must in PATH_OF, f"{must} 没被扫到 —— 排除规则可能收得太紧"


def test_unreachable_modules_declare_wiring_status():
    """生产不可达的模块必须显式声明「未接线」，否则就是未声明的死代码。"""
    missing = [m for m in REPORT.undeclared if m not in ALLOWLIST]
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
        elif m not in UNREACHABLE:
            stale.append(f"{m}: 现在已可达 —— 请从 ALLOWLIST 删除")
        elif REPORT.declared.get(m):
            stale.append(f"{m}: 已带接线声明 —— 请从 ALLOWLIST 删除")
        elif not reason.strip():
            stale.append(f"{m}: 没写理由")
    assert not stale, "ALLOWLIST 需要清理：\n" + "\n".join(f"  {s}" for s in stale)


def test_wired_modules_do_not_carry_stale_declaration():
    """反向：**可达**的模块不该还挂着「未接线」声明（要么接线了忘删，要么措辞不符）。"""
    stale = sorted(
        m for m in PATH_OF
        if m not in UNREACHABLE
        and m not in REACHABLE_BUT_MARKED_KNOWN
        and MARKER_RE.search(PATH_OF[m].read_text(encoding="utf-8", errors="ignore"))
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
        elif not MARKER_RE.search(PATH_OF[m].read_text(encoding="utf-8", errors="ignore")):
            bad.append(f"{m}: 已无接线声明 —— 请从本表删除")
        elif not reason.strip():
            bad.append(f"{m}: 没写理由")
    assert not bad, "REACHABLE_BUT_MARKED_KNOWN 需要清理：\n" + "\n".join(f"  {b}" for b in bad)
