# -*- coding: utf-8 -*-
"""前端声明的接口字段，必须在后端生产代码里**真的出现过**。

缺陷类别：**同义不同名 → 那个字段永远读不到**
----------------------------------------------
`src/api/*.ts` 里的 interface 是**手写的**，与后端实际返回的键之间
**没有任何自动化核对**。写错一个名字的后果是**静默的**：
类型检查过、构建过、运行时读到 `undefined`，界面上某列/某块**永远空白**，
而且不报任何错。

实测（2026-10-09，round 195）：

    Industry40.vue 的 OEE 表有两列绑定 `total_production` / `good_production`，
    而后端 `智能层/oee_calculator.py::calculate_oee` 返回的是
    `total_count` / `good_count` —— **这两列一直是空白的**，
    数据其实就在后端，只是**名字不同**。

口径
----
* 语料 = 后端**生产文本**（`.py/.yaml/.json/.html/.js/...`），
  排除 `.venv` / `dist*` / `build*` / `tests` / 运行期目录。
  ⚠️ **必须含 YAML**：第一版只扫 `.py`，把 `station_output`
  （真身在 `配置/alarms.yaml`）误报成「后端不存在」——
  又是「口径要覆盖全部应当覆盖的对象」。
* 判据是「字段名在后端出现过」这个**必要条件**（不是充分条件）：
  它抓不住「名字对但语义错」，但能抓住「**后端从不产生这个名字**」这一类。
* 少数**故意只在前后端之一存在**的字段进 `ALLOWLIST` 并写明理由；
  allowlist 是**双向**的 —— 已在后端出现却还挂在表上 → 红。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 前端源码定位（与 tests/test_frontend_contract.py 同一套候选）
# ---------------------------------------------------------------------------

_FRONTEND_API_DIR_ENV = "SCADA_FRONTEND_API_DIR"
_FRONTEND_API_DIR_CANDIDATES = (
    BACKEND_ROOT.parent / "scada-app" / "src" / "api",
    BACKEND_ROOT.parent.parent / "scada-app" / "src" / "api",
    Path.home() / "scada-app" / "src" / "api",
)


def _resolve_frontend_api_dir() -> Path:
    env = os.environ.get(_FRONTEND_API_DIR_ENV)
    if env:
        return Path(env)
    for candidate in _FRONTEND_API_DIR_CANDIDATES:
        if candidate.is_dir():
            return candidate
    pytest.fail(
        "未找到前端 api 目录，无法做字段名核对。已尝试：\n"
        + "\n".join(f"  - {p}" for p in _FRONTEND_API_DIR_CANDIDATES)
    )


# ---------------------------------------------------------------------------
# 语料：后端生产文本
# ---------------------------------------------------------------------------

_CORPUS_SKIP_DIRS = {
    "__pycache__", ".venv", "node_modules", ".git", "tests",
    "evidence", "logs", "exports", "data", "backup", "legacy",
}
#: ⚠️ **刻意不含 `.md` / `.txt`**：语料是「后端**代码与配置**」，不是散文。
#: 教科书/分析报告里可以出现任何名字（包括前端专属字段），
#: 把它们算进语料会让判据变弱到「文档里提过就算存在」。
#: 实测：`total_kwh` 只出现在 `SCADA系统教科书.txt` 里 —— 正是靠这条排除，
#: 它才被正确地判为「后端不产生」。
_CORPUS_EXTS = {".py", ".yaml", ".yml", ".json", ".html", ".js", ".css"}
_PROSE_EXTS = {".md", ".txt", ".rst"}


def _iter_corpus_files() -> list[Path]:
    """遍历后端生产文本。

    ⚠️ 用 ``os.walk`` **在遍历时剪枝**，不要 ``rglob('*')`` 之后再过滤 ——
    后者会走进 ``.venv``（几十万文件）再一个个丢掉，实测把这条测试拖到超时。
    """
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(BACKEND_ROOT):
        # 原地改 dirnames 才能真正剪枝（os.walk 会用它决定要不要下去）
        dirnames[:] = [
            d for d in dirnames
            if d not in _CORPUS_SKIP_DIRS
            and not d.startswith(("dist", "build", ".pytest_tmp"))
        ]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in _CORPUS_EXTS:
                out.append(p)
    return out


def _backend_corpus() -> str:
    return "\n".join(
        f.read_text(encoding="utf-8", errors="replace") for f in _iter_corpus_files()
    )


# ---------------------------------------------------------------------------
# 前端声明的字段
# ---------------------------------------------------------------------------

#: `export interface X { … }` 里的**顶层**字段（缩进恰好 2 空格）。
_INTERFACE_RE = re.compile(r"export interface (\w+)\s*\{([\s\S]*?)\n\}", re.M)
_FIELD_RE = re.compile(r"^  (\w+)\??\s*:", re.M)


def _frontend_declared_fields() -> dict[str, list[str]]:
    """返回 ``{"接口名.字段名": [出处…]}``。"""
    out: dict[str, list[str]] = {}
    for f in sorted(_resolve_frontend_api_dir().glob("*.ts")):
        text = f.read_text(encoding="utf-8")
        for m in _INTERFACE_RE.finditer(text):
            iface, body = m.group(1), m.group(2)
            for fld in _FIELD_RE.findall(body):
                out.setdefault(f"{iface}.{fld}", []).append(f.name)
    return out


# ---------------------------------------------------------------------------
# 故意只在前后端之一存在的字段
# ---------------------------------------------------------------------------

#: 进这张表 = 「已知且已判断过」，**不是**「允许随便写」。
ALLOWLIST: dict[str, str] = {
    "AlarmEscalationConfig.escalate_to": (
        "配置页「报警升级」表单的**前端专属**字段。后端没有扁平升级配置段 —— "
        "它的升级模型是**规则列表**（/api/alarms/escalation/rules + AlarmEscalationManager）。"
        "形状不兼容，见决策简报 D14（该表单当前保存必然 400）。"
    ),
    "AlarmEscalationConfig.notify_methods": (
        "配置页「报警升级」表单的**前端专属**字段（通知方式多选）。"
        "后端没有扁平升级配置段 —— 它的模型是**规则列表**"
        "（/api/alarms/escalation/rules + AlarmEscalationManager）。见决策简报 D14。"
    ),
    "RealtimePower.total_kwh": (
        "**有意保留的历史别名**。Industry40.vue 的读取链是 "
        "`num(s.total_energy_kwh ?? s.total_kwh)` —— 主字段 `total_energy_kwh` "
        "在后端真实存在，`total_kwh` 只是兜底别名。"
    ),
}


def test_corpus_is_big_enough():
    """元守卫：语料太小说明口径坏了（那时「都在后端出现过」会变成假绿）。"""
    files = _iter_corpus_files()
    assert len(files) > 100, f"只收到 {len(files)} 个语料文件 —— 口径可能坏了"
    assert any(f.suffix == ".yaml" for f in files), "语料里没有 YAML —— 会漏掉配置里的键"


def test_corpus_excludes_prose():
    """元守卫：散文（.md/.txt）不得混进语料 —— 否则判据弱化成「文档提过就算存在」。

    实测：`total_kwh` 只在 `SCADA系统教科书.txt` 里出现过。
    """
    bad = [f.name for f in _iter_corpus_files() if f.suffix.lower() in _PROSE_EXTS]
    assert not bad, f"语料里混进了散文文件：{bad}"


def test_frontend_fields_are_discovered():
    """元守卫：前端字段扫不出来时，下面的断言会变成空断言。"""
    fields = _frontend_declared_fields()
    assert len(fields) > 100, f"只扫到 {len(fields)} 个字段 —— 口径可能坏了"


def test_every_frontend_field_appears_in_backend():
    """核心断言：每个前端声明的字段名，都必须在后端生产文本里出现过。"""
    corpus = _backend_corpus()
    fields = _frontend_declared_fields()

    missing = {
        name: srcs
        for name, srcs in fields.items()
        if name.split(".", 1)[1] not in corpus and name not in ALLOWLIST
    }

    assert not missing, (
        "以下前端字段在后端生产文本里**完全不出现** —— 后端从不产生这个名字，"
        "运行时读到 undefined（界面某列/某块会永远空白，且不报错）。\n"
        "要么改前端字段名，要么在 ALLOWLIST 里写明为什么它是前端专属的：\n  "
        + "\n  ".join(
            f"{name}  <- {', '.join(srcs)}" for name, srcs in sorted(missing.items())
        )
    )


def test_allowlist_is_not_stale():
    """双向：已经能在后端找到的字段，不该再挂在 ALLOWLIST 上。"""
    corpus = _backend_corpus()
    stale = [
        name for name in ALLOWLIST
        if name.split(".", 1)[1] in corpus
    ]
    assert not stale, (
        f"以下字段已经在后端出现了，请从 ALLOWLIST 删掉：{stale}"
    )


def test_allowlist_entries_have_reasons():
    thin = [k for k, v in ALLOWLIST.items() if len(v.strip()) < 15]
    assert not thin, f"以下 ALLOWLIST 条目没写清理由：{thin}"
