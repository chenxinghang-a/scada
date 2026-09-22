# -*- coding: utf-8 -*-
"""运行时路径不得依赖「当前工作目录」的回归测试（round 173）。

背景：一个反复复发、且**只在打包/服务化部署时才暴露**的缺陷类别
------------------------------------------------------------------
`paths.py` 早就提供了 `paths.resolve()`（基准取 `BASE_DIR`，由 `__file__` /
`sys.executable` 推导，与 CWD 无关），docstring 里也点明了失败模式：

    代码库里散落着 '配置/alarms.yaml'、'data/scada.db' 这类相对路径字面量。
    它们只在「当前工作目录恰好是项目根目录」时才成立 —— 从别处启动
    （系统服务、计划任务、PyInstaller 产物、被别的模块 import）就会找不到文件。
    而且失败方式通常是静默的。

round 170 的 `656ac7a` 修过一轮；round 173 实测打包产物时又抓到一批漏网的。
实测证据（打包后 `resources/backend/`，后端 exe 起服务成功、`/api/health/status` 200）：

    配置验证失败 [devices]: 配置文件不存在: 配置\\devices.yaml
    配置验证失败 [alarms]: 配置文件不存在: 配置\\alarms.yaml
    配置验证失败 [system]: 配置文件不存在: 配置\\system.yaml

配置其实一个不少地躺在 `_internal/配置/` —— 是 `Path('配置')` 相对 CWD 解析错了。
**「配置校验失败」与「路径算错」被混成同一个信号**，校验因此形同虚设。
同类漏网的三处（都在 `core/ops_tools.py`，且都在生产路径上）：

1. 诊断导出（`DiagnosticsExporter`）的 `_collect_config` / `_collect_db_stats` /
   `_copy_recent_logs` 用裸 `Path('配置')` / `Path('data')` / `Path('logs')`
   → **诊断包里 config / 库统计 / 日志三段全空**。
   而诊断包是现场排障时唯一能带走的证据，空包比没有更误导。
2. `OpsAuditLogger` 默认 `log_dir="logs"`，且它在模块尾部被**实例化为全局单例**
   （`ops_audit = OpsAuditLogger()`）—— 也就是说 `import core.ops_tools` 这一步
   就会在**当前工作目录**下 mkdir。
3. `clean_old_backups` / `clean_log_files` 相对路径 → glob 打错目录 →
   匹配不到文件 → 返回 `status: 'success'` + `deleted_count: 0`。
   **「清理成功但一个都没删」是最坏的失败形态**：调用方看到 success 就不会再查。

关于「未接线模块」（重要）
--------------------------
`core/` 下约 60 个模块在文件头标注 `WIRED = False`（生产零引用），
并有守卫用例 `tests/test_core_regressions.py::test_unwired_marker_matches_reality`
保证「标注 ⟺ 事实」。**这些模块里的相对路径不是生产缺陷** ——
它们还没接线，处置方式属 P2-1 口径。本文件的静态守卫因此**自动跳过**
带 `WIRED = False` 的文件，既避免误报，也避免对着"待处置"的代码白做功。

三层守卫
--------
1. `TestPathResolutionContract` —— 相对名 → 绝对目录的映射不变量；
2. `TestCallSitesRouteThroughResolve` —— spy 断言**每个相关函数确实调用了
   `paths.resolve`**（不是"看起来像"，而是"调用链里出现过"）；
3. `TestCwdIndependence` / `TestNoBareRelativePathLiteral` —— 端到端 + 源码兜底。
"""

import ast
import re
from pathlib import Path

import pytest

import paths


# --------------------------------------------------------------------------
# 1. 映射不变量：相对名必须解析到 paths 模块声明的那些绝对目录
# --------------------------------------------------------------------------
class TestPathResolutionContract:
    """"配置"/"data"/"logs"/"exports" 的解析结果必须等于 paths 的对应常量。

    这条不变量一旦破了，`paths.resolve('data')` 与 `paths.DATA_DIR` 就会指向
    两个不同目录 —— 于是"写进去的"和"读出来的"不是同一处。
    """

    @pytest.mark.parametrize("rel,expected", [
        ("配置", "CONFIG_DIR"),
        ("data", "DATA_DIR"),
        ("logs", "LOG_DIR"),
        ("exports", "EXPORT_DIR"),
    ])
    def test_relative_name_maps_to_declared_dir(self, rel, expected):
        assert paths.resolve(rel) == getattr(paths, expected)

    def test_absolute_path_is_passthrough(self, tmp_path):
        """绝对路径必须原样返回 —— 否则调用方传进来的 tmp 目录会被改写。"""
        assert paths.resolve(str(tmp_path)) == tmp_path

    def test_resolution_is_cwd_independent(self, tmp_path, monkeypatch):
        """核心承诺：解析结果与 CWD 无关。"""
        before = paths.resolve('配置')
        monkeypatch.chdir(tmp_path)
        assert paths.resolve('配置') == before


# --------------------------------------------------------------------------
# 2. 调用点必须真的走 paths.resolve（spy 断言，不靠肉眼）
# --------------------------------------------------------------------------
@pytest.fixture
def resolve_spy(monkeypatch):
    """记录所有经过 paths.resolve 的入参，同时保留真实行为。"""
    calls = []
    real = paths.resolve

    def spy(p):
        calls.append(str(p))
        return real(p)

    monkeypatch.setattr(paths, "resolve", spy)
    return calls


class TestCallSitesRouteThroughResolve:
    """逐个断言「该走 resolve 的地方确实走了」。

    为什么用 spy 而不是断言返回值：这些函数多数返回内部状态或聚合结果，
    从返回值看不出"路径是怎么解析的"。spy 直接观测调用链，最贴近要守的规则。
    """

    def test_config_validator_resolves_config_dir(self, resolve_spy):
        from core import config_validator as mod

        mod.validate_all_configs()
        assert '配置' in resolve_spy, (
            "core/config_validator.validate_all_configs 必须经 paths.resolve 解析 config_dir；"
            "直接 Path('配置') 会让打包产物永远报「配置文件不存在」"
        )

    def test_ops_audit_logger_resolves_log_dir(self, resolve_spy):
        from core import ops_tools as mod

        logger = mod.OpsAuditLogger()
        assert 'logs' in resolve_spy
        assert logger._log_dir == paths.LOG_DIR

    def test_ops_audit_logger_dir_is_not_cwd_relative(self, tmp_path, monkeypatch):
        """模块级单例在 import 时就 mkdir —— 必须不落在 CWD。"""
        from core import ops_tools as mod

        monkeypatch.chdir(tmp_path)
        logger = mod.OpsAuditLogger()
        assert not (tmp_path / 'logs').exists(), (
            "OpsAuditLogger 把日志目录建到了当前工作目录下"
        )
        assert logger._log_dir.is_relative_to(paths.PROJECT_ROOT)

    def test_diagnostics_collectors_resolve(self, resolve_spy, tmp_path):
        from core import ops_tools as mod

        diag_dir = tmp_path / 'diag'
        diag_dir.mkdir()
        exporter = mod.DiagnosticExporter(output_dir=str(diag_dir))
        exporter._collect_config()
        exporter._collect_db_stats()
        exporter._copy_recent_logs(diag_dir)

        for expected in ('配置', 'data', 'logs'):
            assert expected in resolve_spy, (
                f"诊断导出收集 {expected} 时必须经 paths.resolve；"
                "否则打包后诊断包这几段恒为空"
            )

    def test_cleanup_helpers_resolve(self, resolve_spy):
        from core import ops_tools as mod

        cleaner = mod.DataCleaner()
        cleaner.clean_old_backups()
        cleaner.clean_log_files()
        assert 'data' in resolve_spy and 'logs' in resolve_spy


# --------------------------------------------------------------------------
# 3. 端到端：换一个 CWD，关键功能仍要工作
# --------------------------------------------------------------------------
class TestCwdIndependence:
    """在临时目录下当 CWD，验证真实行为（而不是只验证"调了 resolve"）。"""

    def test_config_validation_finds_real_configs(self, tmp_path, monkeypatch):
        """换 CWD 后配置校验仍应通过。

        这是打包产物那条日志的直接回归：修复前只要 CWD 不是仓库根，
        三份配置都会被判「不存在」。
        """
        from core import config_validator as mod

        monkeypatch.chdir(tmp_path)
        results = mod.validate_all_configs()

        missing = [k for k, (ok, errs) in results.items()
                   if not ok and any('不存在' in e for e in errs)]
        assert not missing, f"换 CWD 后这些配置被判为不存在（路径解析问题）: {missing}"

    def test_no_runtime_dir_created_in_cwd(self, tmp_path, monkeypatch):
        """调用关键函数后，CWD 下不得多出 logs/ data/ 之类目录。"""
        from core import ops_tools as mod

        monkeypatch.chdir(tmp_path)
        mod.OpsAuditLogger()
        diag_dir = tmp_path / 'diag'
        diag_dir.mkdir()
        mod.DiagnosticExporter(output_dir=str(diag_dir))
        mod.DataCleaner().clean_log_files()

        for name in ('logs', 'data', '配置'):
            assert not (tmp_path / name).exists(), (
                f"CWD 下被创建了 {name}/ —— 说明某处仍在用相对路径"
            )


# --------------------------------------------------------------------------
# 4. 源码级兜底：禁止裸相对目录字面量
# --------------------------------------------------------------------------
#: 裸相对目录：Path('配置') / Path("data") 之类。
_BARE_DIR = re.compile(r"""Path\(\s*['"](配置|data|logs|exports|模板|静态资源)['"]\s*\)""")

#: 未接线模块的标注头。带这个标注的模块生产零引用，其内部相对路径
#: **不是生产缺陷**（处置属 P2-1），静态守卫直接跳过。
_UNWIRED_MARKER = 'WIRED = False'

_SKIP_DIRS = {'.venv', 'node_modules', 'legacy', 'tests', '测试',
              '.git', '__pycache__', 'scada-app', '.pytest_tmp-0'}


def _production_py_files():
    """枚举需要检查的生产代码文件（排除测试、产物、工具链缓存）。"""
    root = Path(paths.PROJECT_ROOT)
    for p in root.rglob('*.py'):
        rel_parts = p.relative_to(root).parts
        if set(rel_parts) & _SKIP_DIRS:
            continue
        if any(seg.startswith('.pytest_tmp') for seg in rel_parts):
            continue
        # 构建产物目录整棵跳过（`dist-scada-*/`、`build-scada-*/`、`release-*/`…）。
        # 它们是源码的**逐字副本**，扫到就会对着产物报违规 = 假红，
        # 而假红会训练人「看到守卫红了就忽略」。权威清单是仓库 `.gitignore`，
        # 这里的前缀表是它的镜像（2026-09-22 实测：PyInstaller 输出
        # `dist-scada-1.3.1037/` 曾让另两条静态守卫假红）。
        if any(seg.startswith(('dist', 'build', 'release', 'backup')) for seg in rel_parts):
            continue
        yield p.relative_to(root).as_posix(), p


def _docstring_lines(source: str) -> set[int]:
    """用 AST 找出模块/类/函数 docstring 覆盖的行号。

    为什么必须用 AST：文档里**举例**说明坏写法（``Path('配置')``）是正常的，
    按行首三引号判断会漏掉缩进过的 docstring 正文。
    """
    lines: set[int] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return lines
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, 'body', None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for ln in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                lines.add(ln)
    return lines


class TestNoBareRelativePathLiteral:
    """源码扫描：生产代码里不得出现**未解析**的裸相对目录。

    这是"兜底网"，不是主力 —— 主力是上面的 spy 断言（它能抓住
    `Path(config_dir)` 这种字面量扫不到、但默认值仍是相对路径的形态）。
    它的价值在于：**新增**代码时立刻红灯，而不是等某次打包实测才发现。
    """

    def test_no_bare_relative_dir_in_path_call(self):
        offenders = []
        skipped_unwired = []
        for rel, p in _production_py_files():
            source = p.read_text(encoding='utf-8', errors='replace')
            if _UNWIRED_MARKER in source:
                skipped_unwired.append(rel)
                continue
            doc_lines = _docstring_lines(source)
            for i, line in enumerate(source.splitlines(), 1):
                if i in doc_lines or line.strip().startswith('#'):
                    continue
                if _BARE_DIR.search(line):
                    offenders.append(f"{rel}:{i}: {line.strip()}")

        assert not offenders, (
            "发现裸相对目录字面量（应改为 paths.resolve(...)）:\n  "
            + "\n  ".join(offenders)
            + f"\n（已按 WIRED = False 跳过 {len(skipped_unwired)} 个未接线模块）"
        )

    def test_unwired_skip_list_is_not_silently_growing(self):
        """跳过未接线模块是刻意的，但数量不该失控。

        这条不是硬性上限，而是一个**提醒**：跳过的模块数突然变大，
        说明有人用 `WIRED = False` 把问题盖掉了，而不是修掉。
        """
        unwired = [rel for rel, p in _production_py_files()
                   if _UNWIRED_MARKER in p.read_text(encoding='utf-8', errors='replace')]
        assert len(unwired) < 120, (
            f"被跳过的未接线模块已达 {len(unwired)} 个，明显偏多，请确认不是滥用标注"
        )

    def test_scan_excludes_build_output(self):
        """构建产物目录不得进入扫描集 —— 假红比漏报更危险。

        回归背景（2026-09-22）：本地跑完 PyInstaller（输出 `dist-scada-1.3.1037/`）后，
        `test_silent_exception_guard.py` 与 `test_timestamp_sql_consistency.py` 双双假红
        —— 报的全是**构建产物里的源码副本**。假红会训练人「看到守卫红了就忽略」，
        等于把守卫废掉。本文件当时已按前缀跳过产物，故未受影响；
        这条测试把该性质钉住，并覆盖 `release-*` / `backup*` 等同类目录。
        """
        scanned = list(_production_py_files())
        assert scanned, "扫描集为空，守卫已失效"
        offenders = [
            rel for rel, _ in scanned
            if any(seg.startswith(('dist', 'build', 'release', 'backup'))
                   for seg in Path(rel).parts)
        ]
        assert not offenders, f"扫描集混入构建产物: {offenders[:5]}"
