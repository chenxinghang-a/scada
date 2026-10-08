# -*- mode: python ; coding: utf-8 -*-
#
# 后端 PyInstaller 打包配方。
#
# 为什么这个文件必须进 git：此前它被 .gitignore 的 `*.spec` 规则挡在外面
# （磁盘上有、git 里没有），导致**新克隆的仓库无法复现后端打包**。
# 现已在 .gitignore 里加 `!scada-backend.spec` 例外放行。
#
# pathex 为什么不用硬编码：原来是 'C:\Users\cxx\WorkBuddy\Claw\industrial_scada'，
# 换机器 / 换用户名就构建失败。SPECPATH 是 PyInstaller 注入的全局变量，
# 等于 os.path.split(SPEC)[0]（即 spec 文件所在目录）。
# 官方文档：https://pyinstaller.org/en/stable/spec-files.html
#           「Globals Available to the Spec File」→ SPECPATH
# 注意：SPECPATH 可能是相对路径（从仓库根目录执行时为空串），故用 abspath 归一。
#
# ⚠️ 运行时**要读的文件**必须全部出现在 datas 里，否则冻结产物里没有它，
#    而缺失的失败方式通常是**静默的**（代码里 try/except 回退到默认值）。
#
#    `VERSION` 就踩过这个坑（2026-10-08 修复，round 191）：
#    `core/version.py` 用 `Path(__file__).parent.parent / 'VERSION'` 定位，
#    冻结布局下即 `_internal/VERSION`；而 datas 里没有它 →
#    `get_version()` 返回 `"0.0.0-unknown"` →
#    **打包产物在 `/api/health/status`、`/api/system/status`、Swagger、
#    Prometheus 指标里一致自报 `0.0.0-unknown`**，而开发环境一切正常
#    （仓库根就有 VERSION 文件）。本机实测证据见
#    `SCADA-round191-打包产物版本号静默失效-20261008.md`。
#
#    现在由两道闸门看着它，别再漏：
#      1. `tests/test_packaging_data_coverage.py` —— 静态断言 VERSION 在 datas 里，
#         且 `core/version.py` 的定位基准与 datas 的落点一致；
#      2. CI 的打包冒烟脚本 `--expect-version` —— 在**真产物**上断言自报版本 ==
#         源码 VERSION，而不是只看 HTTP 200。
import os

a = Analysis(
    ['run.py'],
    pathex=[os.path.abspath(SPECPATH)],
    binaries=[],
    datas=[('run.py', '.'), ('VERSION', '.'), ('模板', '模板'), ('静态资源', '静态资源'), ('配置', '配置'), ('core', 'core'), ('采集层', '采集层'), ('存储层', '存储层'), ('报警层', '报警层'), ('展示层', '展示层'), ('智能层', '智能层'), ('用户层', '用户层'), ('timeseries', 'timeseries'), ('tools', 'tools'), ('paths.py', '.'), ('config.py', '.')],
    hiddenimports=['run', 'flask', 'flask_socketio', 'socketio', 'engineio', 'engineio.async_drivers.threading', 'engineio.async_drivers._websocket_wsgi', 'pymodbus', 'asyncua', 'paho.mqtt', 'jwt', 'bcrypt', 'pandas', 'numpy', 'yaml', 'loguru', 'apscheduler', 'openpyxl', 'dotenv', 'requests'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PIL'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='scada-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='scada-backend',
)
