# -*- coding: utf-8 -*-
"""冻结（PyInstaller onedir）布局下的路径与版本号回归守卫。

缺陷类别：**本机绿、产物错**
--------------------------------
``config.py`` 曾经用 ``paths.PROJECT_ROOT`` 作为 ``BASE_DIR``。
在开发布局下 ``PROJECT_ROOT == BASE_DIR``（仓库根），所以一切正常；
但在冻结布局下两者**差一级**：

    PROJECT_ROOT = exe 所在目录          ``<backend>/``
    BASE_DIR     = 运行时数据基准目录     ``<backend>/_internal/``

后果（2026-10-08 修复，round 191）：

* ``DatabaseConfig.DB_PATH`` / ``ExportConfig.EXPORT_DIR`` / ``LogConfig.LOG_DIR``
  在产物里全部指向 ``_internal/`` **外面**，与 ``paths.DATA_DIR`` /
  ``paths.EXPORT_DIR`` / ``paths.LOG_DIR`` 静默分叉；
* ``_read_version()`` 去 ``<backend>/VERSION`` 找版本号 —— 那里没有，
  于是 ``config.APP_VERSION`` 恒为 ``'0.0.0'``。

为什么必须在**子进程**里伪造
--------------------------------
这两个常量是**模块级**的，在开发机上 ``_internal`` 不存在，无论怎么断言
``config.X == paths.X`` 都成立 —— 那种测试是**瞎的**（改坏了也不会红）。
所以这里在一个独立子进程里：造一个带 ``_internal/`` 的假根目录、
把 ``sys.frozen`` 打开、把 ``sys.executable`` 指过去，
再让 ``paths`` / ``config`` 在那个环境里被**首次导入**。
这样本机也能跑出产物的行为，且不污染当前 pytest 进程的模块状态。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: 在假根目录里跑的探针。**注意**：所有 import 必须发生在
#: ``sys.frozen`` / ``sys.executable`` / ``sys.path`` 都设好之后。
_PROBE = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path

    root = Path(sys.argv[1]).resolve()

    # ---- 伪造冻结环境（顺序重要：import 之前） ----
    sys.frozen = True
    sys.executable = str(root / "scada-backend.exe")
    sys.path.insert(0, str(root / "_internal"))

    import paths
    import config

    # `core.version` 刻意**按文件路径**加载，不走 `import core.version`：
    # `core/__init__.py` 会拉起 di_container 等一堆重依赖，与本测试无关。
    # 按路径加载时 `version.py` 的 `__file__` 仍是 `_internal/core/version.py`，
    # 与真实产物里一致 —— 它内部 `Path(__file__).parent.parent` 的推导照样被测到。
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "frozen_probe_version", root / "_internal" / "core" / "version.py")
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    get_version = _mod.get_version

    print("__PROBE__" + json.dumps({
        "paths_project_root": str(paths.PROJECT_ROOT),
        "paths_base_dir": str(paths.BASE_DIR),
        "paths_data_dir": str(paths.DATA_DIR),
        "paths_log_dir": str(paths.LOG_DIR),
        "paths_export_dir": str(paths.EXPORT_DIR),
        "config_base_dir": str(config.BASE_DIR),
        "config_db_path": str(config.DatabaseConfig.DB_PATH),
        "config_log_dir": str(config.LogConfig.LOG_DIR),
        "config_export_dir": str(config.ExportConfig.EXPORT_DIR),
        "config_app_version": config.APP_VERSION,
        "core_version": get_version(),
    }, ensure_ascii=False))
    """
)

#: 放进假 ``_internal/VERSION`` 的哨兵版本号。刻意与仓库真实 VERSION 不同，
#: 这样「读到了真实仓库的 VERSION」会被区分出来。
SENTINEL_VERSION = "9.9.9-frozen-probe"


@pytest.fixture(scope="module")
def frozen_probe(tmp_path_factory) -> dict:
    """造一个冻结布局的假根目录，在子进程里导入 paths/config，返回探针输出。"""
    root = tmp_path_factory.mktemp("frozen-root")
    internal = root / "_internal"
    internal.mkdir()

    # datas 的落点（见 spec）：paths.py / config.py / VERSION / core 都在 _internal 下
    shutil.copy2(_REPO_ROOT / "paths.py", internal / "paths.py")
    shutil.copy2(_REPO_ROOT / "config.py", internal / "config.py")
    (internal / "VERSION").write_text(SENTINEL_VERSION + "\n", encoding="utf-8")

    (internal / "core").mkdir()
    shutil.copy2(_REPO_ROOT / "core" / "__init__.py", internal / "core" / "__init__.py")
    shutil.copy2(_REPO_ROOT / "core" / "version.py", internal / "core" / "version.py")

    probe_file = root / "_probe.py"
    probe_file.write_text(_PROBE, encoding="utf-8")

    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    # 这几个环境变量会覆盖 config 里的默认值，必须先摘掉，否则测的不是默认推导
    for key in ("SCADA_LOG_DIR", "SCADA_CONFIG_DIR", "SCADA_ENV"):
        env.pop(key, None)

    proc = subprocess.run(
        [sys.executable, str(probe_file), str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(root),
        env=env,
    )

    assert proc.returncode == 0, (
        "冻结布局探针自身跑挂了（不是被测代码的问题，先修探针）\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("__PROBE__")), None
    )
    assert line is not None, f"探针没打出结果行\nstdout:\n{proc.stdout}"

    out = json.loads(line[len("__PROBE__"):])
    out["_root"] = str(root)
    out["_internal"] = str(internal)
    return out


class TestFrozenLayoutIsActuallySimulated:
    """正向对照：先证明「冻结环境确实生效了」，否则下面所有断言都是空的。"""

    def test_frozen_branch_taken(self, frozen_probe):
        # 开发布局下 PROJECT_ROOT 是仓库根；冻结布局下是假根目录。
        assert frozen_probe["paths_project_root"] == frozen_probe["_root"], (
            "paths 没走 frozen 分支 —— 这个测试就没在测冻结布局，"
            "下面的断言全是空的"
        )

    def test_base_dir_is_internal(self, frozen_probe):
        assert frozen_probe["paths_base_dir"] == frozen_probe["_internal"], (
            "paths.BASE_DIR 应当落在 _internal/ 下"
        )


class TestConfigAgreesWithPathsInFrozenLayout:
    """核心回归：冻结布局下 config 的路径必须与 paths 一致。"""

    def test_config_base_dir_is_internal(self, frozen_probe):
        """⚠️ 这条就是 round 191 修的缺陷本身。"""
        assert frozen_probe["config_base_dir"] == frozen_probe["_internal"], (
            "config.BASE_DIR 不是 _internal/ —— "
            "说明它又用 paths.PROJECT_ROOT 推导了（见 config.py 顶部注释）"
        )

    def test_db_path_under_data_dir(self, frozen_probe):
        assert frozen_probe["config_db_path"] == str(
            Path(frozen_probe["paths_data_dir"]) / "scada.db"
        ), "config 的 DB_PATH 与 paths.DATA_DIR 分叉了"

    def test_log_dir_under_paths_log_dir(self, frozen_probe):
        assert frozen_probe["config_log_dir"] == frozen_probe["paths_log_dir"], (
            "config 的 LOG_DIR 与 paths.LOG_DIR 分叉 —— "
            "会导致「日志写在一处、排障工具看另一处」"
        )

    def test_export_dir_under_paths_export_dir(self, frozen_probe):
        assert frozen_probe["config_export_dir"] == frozen_probe["paths_export_dir"], (
            "config 的 EXPORT_DIR 与 paths.EXPORT_DIR 分叉"
        )


class TestVersionLookupInFrozenLayout:
    """冻结布局下版本号必须读得到（VERSION 由 spec 的 datas 落到 _internal/）。"""

    def test_core_version_reads_bundled_version(self, frozen_probe):
        assert frozen_probe["core_version"] == SENTINEL_VERSION, (
            "core/version.py 没从 _internal/VERSION 读到哨兵版本 —— "
            "它算的基准目录与 spec datas 的落点对不上"
        )

    def test_config_app_version_reads_bundled_version(self, frozen_probe):
        assert frozen_probe["config_app_version"] == SENTINEL_VERSION, (
            "config.APP_VERSION 没读到 _internal/VERSION "
            "（修复前恒为 '0.0.0'）"
        )

    def test_never_reports_unknown_marker(self, frozen_probe):
        assert "unknown" not in frozen_probe["core_version"], (
            "产物自报 0.0.0-unknown —— VERSION 没被打进产物"
        )
