"""
打包脚本：SCADA 系统 → 单个 exe（自动开浏览器）

零删除设计（勿改）：
    本机环境注入了批量删除保护，`shutil.rmtree('dist')` 之类的清理会被
    弹窗拦截（SAFE_DELETE_BULK_CONFIRM_REQUIRED → SystemExit 1）。
    因此本脚本**从不删除任何目录**，改为每次构建输出到带版本号的独立目录：

        构建产物   dist-scada-<VERSION>/SCADA.exe
        工作目录   build-scada-<VERSION>/

    版本变了就换个目录，天然隔离；同版本重复构建时用
    PyInstaller 自带的 ``--noconfirm`` 让它自己覆盖同名文件
    （PyInstaller 覆盖不触发环境的批量删除保护，它走的是单文件写路径）。

    不再需要"先 mv 成 .prev 再删"这种打补丁手法。
"""

import subprocess
import sys
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent


def _read_version() -> str:
    """读取 VERSION 文件（与后端运行时同一真源）。"""
    vf = PROJECT_ROOT / 'VERSION'
    try:
        return vf.read_text(encoding='utf-8').strip() or 'dev'
    except OSError:
        return 'dev'


def build():
    version = _read_version()
    tag = f'scada-{version}'
    # 带版本的独立目录：换版本即换目录，不需要删除任何东西
    dist_dir = PROJECT_ROOT / f'dist-{tag}'
    work_dir = PROJECT_ROOT / f'build-{tag}'
    spec_dir = work_dir  # 每次构建生成 spec 也放工作目录，不污染仓库根

    print("=" * 50)
    print("  SCADA 系统打包")
    print(f"  版本: {version}")
    print(f"  输出: {dist_dir.relative_to(PROJECT_ROOT)}")
    print("=" * 50)

    subprocess.run([sys.executable, '-m', 'pip', 'install', 'pyinstaller', '-q'])

    print("\n打包中...")

    data_dirs = [
        '模板', '静态资源', '配置', 'core', '采集层', '存储层',
        '报警层', '展示层', '智能层', '用户层', 'timeseries', 'tools',
    ]

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile', '--name', 'SCADA', '--console', '--noconfirm',
        # 关键：显式指定输出/工作/配置目录，避免落到默认的 dist/ 与 build/
        '--distpath', str(dist_dir),
        '--workpath', str(work_dir),
        '--specpath', str(spec_dir),
    ]

    for d in data_dirs:
        dp = PROJECT_ROOT / d
        if dp.exists():
            cmd.extend(['--add-data', f'{dp};{d}'])

    for f in ['paths.py', 'config.py']:
        fp = PROJECT_ROOT / f
        if fp.exists():
            cmd.extend(['--add-data', f'{fp};.'])

    for imp in ['flask', 'flask_socketio', 'socketio', 'engineio',
                'pymodbus', 'asyncua', 'paho.mqtt', 'jwt', 'bcrypt',
                'pandas', 'numpy', 'yaml', 'loguru', 'apscheduler',
                'openpyxl', 'dotenv', 'requests']:
        cmd.extend(['--hidden-import', imp])

    for exc in ['tkinter', 'matplotlib', 'PIL']:
        cmd.extend(['--exclude-module', exc])

    cmd.append('launcher.py')
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))

    exe = dist_dir / 'SCADA.exe'
    if result.returncode == 0 and exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        for d in ['data', 'logs', 'exports']:
            (dist_dir / d).mkdir(exist_ok=True)
        if not (dist_dir / '.env').exists() and (PROJECT_ROOT / '.env.example').exists():
            shutil.copy2(PROJECT_ROOT / '.env.example', dist_dir / '.env')

        print(f"\n{'=' * 50}")
        print(f"  打包成功: {exe}")
        print(f"  大小: {size_mb:.1f} MB")
        print(f"  双击 SCADA.exe 自动开浏览器")
        print(f"{'=' * 50}")
    else:
        print("\n打包失败")


if __name__ == '__main__':
    build()
