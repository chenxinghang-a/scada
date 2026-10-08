# -*- coding: utf-8 -*-
"""打包 datas 覆盖度守卫：**运行时按路径读的文件必须真的进产物**。

缺陷类别：fail-open 闸门 + 静默失效
------------------------------------
``VERSION`` 长期不在 ``scada-backend.spec`` 的 ``datas`` 里。
缺它的失败方式**不是**报错，而是：

    ``core/version.py::get_version()`` 读不到文件 → 返回 ``"0.0.0-unknown"``
    → 冻结产物在 ``/api/health/status`` / ``/api/system/status`` / Swagger /
      Prometheus 指标里**一致自报一个不存在的版本**

而开发机上仓库根就有 ``VERSION``，所以**本机永远看不出来**。
``build-backend`` 的冒烟闸门当时只断言「能起、模块就绪」，不看版本号
—— 闸门本身是 fail-open 的。2026-10-08（round 191）实测坐实并修复。

本文件锁三件事：

1. **datas 覆盖** —— ``VERSION`` 必须出现在两个 spec 的 ``datas`` 里；
2. **定位基准一致** —— ``core/version.py`` 用 ``Path(__file__).parent.parent``
   找 VERSION，所以「``core`` 的落点」与「``VERSION`` 的落点」必须**指向同一层**。
   只断言「VERSION 在列表里」是不够的：落点写成 ``'data'`` 一样能过，但运行时找不到；
3. **接线** —— CI 必须真的把源码 VERSION 传给冒烟脚本（``--expect-version``），
   否则脚本加了参数也没人用。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_SPEC = _REPO_ROOT / "scada-backend.spec"
_LEGACY_SPEC = _REPO_ROOT / "SCADA.spec"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_SMOKE_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "smoke_packaged_backend.py"


# ---------------------------------------------------------------------------
# datas 解析（纯函数，单独测）
# ---------------------------------------------------------------------------

def _eval_entry_part(node: ast.expr) -> str:
    """求值 datas 元组里的一个元素：字面量，或 ``_p('a', 'b')`` 调用。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_p"
    ):
        parts = []
        for arg in node.args:
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                raise AssertionError(f"_p() 只支持字符串字面量: {ast.dump(arg)}")
            parts.append(arg.value)
        return "/".join(parts)
    raise AssertionError(f"不认识的 datas 元素表达式: {ast.dump(node)}")


def parse_datas(spec_source: str) -> list[tuple[str, str]]:
    """从 spec 源码里取出 ``Analysis(datas=[...])`` 的 ``[(src, dest), ...]``。

    刻意不 import spec：spec 文件用了 PyInstaller 注入的全局变量（``SPECPATH``），
    import 会失败。静态解析反而更稳，也更贴近「检查配方文本」的意图。
    """
    tree = ast.parse(spec_source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "Analysis"):
            continue
        for kw in node.keywords:
            if kw.arg != "datas":
                continue
            if not isinstance(kw.value, ast.List):
                raise AssertionError("datas 不是列表字面量，本解析器无法处理")
            out: list[tuple[str, str]] = []
            for elt in kw.value.elts:
                if not (isinstance(elt, ast.Tuple) and len(elt.elts) == 2):
                    raise AssertionError(f"datas 元素不是二元组: {ast.dump(elt)}")
                out.append((_eval_entry_part(elt.elts[0]),
                            _eval_entry_part(elt.elts[1])))
            return out
    raise AssertionError("spec 里找不到 Analysis(datas=[...])")


class TestParseDatas:
    """守卫自己也要验：解析器在合成输入上的行为。"""

    def test_parses_plain_tuples(self):
        src = "a = Analysis(['run.py'], datas=[('run.py', '.'), ('模板', '模板')])"
        assert parse_datas(src) == [("run.py", "."), ("模板", "模板")]

    def test_parses_helper_calls(self):
        src = "a = Analysis(['l.py'], datas=[(_p('VERSION'), '.'), (_p('core'), 'core')])"
        assert parse_datas(src) == [("VERSION", "."), ("core", "core")]

    def test_ignores_other_keywords(self):
        src = (
            "a = Analysis(['x'], pathex=['/tmp'], datas=[('V', '.')], "
            "hiddenimports=['flask'])"
        )
        assert parse_datas(src) == [("V", ".")]

    def test_rejects_unknown_expression(self):
        """解析器**不能**对不认识的形状静默返回空 —— 那会让守卫变瞎。"""
        src = "a = Analysis(['x'], datas=[('a' + 'b', '.')])"
        with pytest.raises(AssertionError):
            parse_datas(src)

    def test_missing_datas_raises(self):
        with pytest.raises(AssertionError):
            parse_datas("a = Analysis(['x'], pathex=['/tmp'])")


# ---------------------------------------------------------------------------
# 两个 spec 的 datas 覆盖
# ---------------------------------------------------------------------------

def _datas_of(path: Path) -> list[tuple[str, str]]:
    return parse_datas(path.read_text(encoding="utf-8"))


def _legacy_spec_or_skip() -> Path:
    """返回 ``SCADA.spec``；**不在磁盘上就跳过**。

    ⚠️ ``SCADA.spec`` **不在仓库里** —— ``.gitignore`` 第 66 行是 ``*.spec``，
    只有 ``scada-backend.spec`` 有例外放行（第 69 行 ``!scada-backend.spec``）。
    也就是说 **CI 的干净检出里根本没有这个文件**。

    第一版守卫直接 ``read_text()``，在本机（文件存在）绿、在 CI 上
    ``FileNotFoundError`` —— 正是本仓库反复踩的「本机绿 / CI 红」。
    已用「把文件改名、本地复现 CI 布局」的方式实测确认过。

    所以这里显式跳过并说明原因：这条断言只对**本机持有该文件**的人有意义。
    「是否把 SCADA.spec 纳入仓库」属仓库策略，已记进决策简报，不在这里判负。
    """
    if not _LEGACY_SPEC.is_file():
        pytest.skip(
            "SCADA.spec 不在仓库里（.gitignore 的 *.spec 规则，仅 scada-backend.spec "
            "例外放行）—— 干净检出下不存在，故跳过；该断言只对本机有效"
        )
    return _LEGACY_SPEC


class TestVersionIsBundled:
    """``VERSION`` 必须在 datas 里 —— 否则产物自报 0.0.0-unknown。"""

    def test_backend_spec_bundles_version(self):
        datas = _datas_of(_BACKEND_SPEC)
        # 元守卫：解析结果不能是空/极小集合，否则下面的断言可能因为「没解析到」而假绿
        assert len(datas) >= 10, f"只解析出 {len(datas)} 条 datas，解析器可能坏了"
        assert ("VERSION", ".") in datas, (
            "scada-backend.spec 的 datas 里没有 ('VERSION', '.') —— "
            "冻结产物会自报 0.0.0-unknown"
        )

    def test_legacy_spec_bundles_version(self):
        datas = _datas_of(_legacy_spec_or_skip())
        assert len(datas) >= 10, f"只解析出 {len(datas)} 条 datas，解析器可能坏了"
        assert any(src == "VERSION" for src, _ in datas), (
            "SCADA.spec 的 datas 里没有 VERSION"
        )

    def test_ci_relevant_spec_is_tracked(self):
        """``scada-backend.spec``（CI 用的那个）**必须**在仓库里。

        与上一条相反：这个文件如果不在仓库，CI 根本没法打包后端。
        round 161 专门在 .gitignore 里加了 ``!scada-backend.spec`` 例外放行，
        这条守卫防止那条例外被误删。
        """
        gitignore = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        lines = [ln.strip() for ln in gitignore.splitlines()]
        assert "!scada-backend.spec" in lines, (
            ".gitignore 里缺少 '!scada-backend.spec' 例外 —— "
            "干净检出将没有打包配方，CI 无法复现后端产物"
        )
        assert _BACKEND_SPEC.is_file(), "scada-backend.spec 不在磁盘上"


class TestVersionLookupBaseMatchesDatasDestination:
    """``core/version.py`` 的定位基准必须与 datas 的落点指向同一层。

    ``core/version.py`` 里：``_ROOT = Path(__file__).resolve().parent.parent``。
    datas 把 ``core`` 放到 ``core`` → 模块落在 ``<MEIPASS>/core/version.py``
    → ``_ROOT = <MEIPASS>``。所以 VERSION 的 dest 必须解析到 ``<MEIPASS>``（即 ``'.'``）。
    """

    def test_core_destination_is_core(self):
        datas = dict(_datas_of(_BACKEND_SPEC))
        assert datas.get("core") == "core", (
            "core 的 datas 落点不是 'core'，_ROOT 的推导前提就不成立了"
        )

    def test_version_destination_matches_core_parent(self):
        datas = dict(_datas_of(_BACKEND_SPEC))
        core_dest = datas.get("core")
        version_dest = datas.get("VERSION")
        assert version_dest is not None, "VERSION 不在 datas 里"

        # 把落点归一化成路径，再比较「VERSION 所在目录」与「core 的父目录」
        def _norm(dest: str) -> str:
            return "" if dest in (".", "") else dest.strip("/")

        core_dir = _norm(core_dest)
        version_dir = _norm(version_dest)
        # core_dir = 'core' → 其父目录是 ''
        core_parent = core_dir.rsplit("/", 1)[0] if "/" in core_dir else ""
        assert version_dir == core_parent, (
            f"VERSION 落点 {version_dest!r} 与 core 落点 {core_dest!r} 不同层 —— "
            "core/version.py 的 parent.parent 会找不到 VERSION"
        )


# ---------------------------------------------------------------------------
# 接线
# ---------------------------------------------------------------------------

class TestSmokeGateWiring:
    """脚本加了参数，CI 必须真的用上，否则等于没加。

    ⚠️ 这里**必须先剥掉整行注释再断言**。第一版守卫直接对整份 yml 做子串匹配，
    结果被我写在同一段里的说明性注释（也含 ``--expect-version`` 字样）喂饱了 ——
    把真实调用里的参数删掉，守卫照样绿。**只断言「存在」= 守不住。**
    """

    @staticmethod
    def _strip_comment_lines(text: str) -> str:
        """去掉整行注释（`lstrip()` 后以 `#` 开头的行）。

        只去整行注释，不处理行尾内联注释 —— 本仓库的 workflow 里没有那种写法。
        """
        return "\n".join(
            ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
        )

    def test_smoke_script_declares_expect_version_flag(self):
        text = _SMOKE_SCRIPT.read_text(encoding="utf-8")
        # 断言的是**真的注册了这个命令行参数**，不是「文本里出现过这个词」
        assert 'add_argument("--expect-version"' in text, (
            "冒烟脚本没有注册 --expect-version 命令行参数"
        )
        assert "def assess_version(" in text, "冒烟脚本没有 assess_version 判定函数"

    def test_ci_passes_source_version(self):
        raw = _CI_YML.read_text(encoding="utf-8")
        code = self._strip_comment_lines(raw)

        assert "smoke_packaged_backend.py" in code, "CI 没调用冒烟脚本"
        assert "--expect-version" in code, (
            "CI 调用冒烟脚本时没传 --expect-version —— 版本断言在 CI 上等于没开"
        )
        # 期望值必须来自**源码 VERSION 文件**，不能是别处硬编码的常量
        assert "VERSION" in code, "CI 里没有从 VERSION 文件取期望值的痕迹"

    def test_ci_flag_is_not_only_in_comments(self):
        """元守卫：证明「剥注释」这一步真的在起作用。

        如果注释里根本没有这些字样，上一条断言就退化成普通子串匹配，
        剥注释这一步也就没被验证过。
        """
        raw = _CI_YML.read_text(encoding="utf-8")
        code = self._strip_comment_lines(raw)
        assert raw != code, "CI yml 里没有整行注释，剥注释逻辑未被验证"
        assert "--expect-version" in raw and "--expect-version" in code

    def test_smoke_script_defines_unknown_marker_constant(self):
        text = _SMOKE_SCRIPT.read_text(encoding="utf-8")
        assert 'UNKNOWN_VERSION_MARKER = "0.0.0-unknown"' in text, (
            "冒烟脚本里没有 UNKNOWN_VERSION_MARKER 常量 —— "
            "产物自报 unknown 时不会给出针对性提示"
        )
