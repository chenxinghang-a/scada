"""
打包脚本：SCADA 系统 → 单个 exe（自动开浏览器）
"""

import subprocess
import sys
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent


def build():
    print("=" * 50)
    print("  SCADA 系统打包")
    print("=" * 50)

    subprocess.run([sys.executable, '-m', 'pip', 'install', 'pyinstaller', '-q'])

    for d in ['dist', 'build']:
        p = PROJECT_ROOT / d
        if p.exists():
            shutil.rmtree(p)

    print("\n打包中...")

    data_dirs = [
        '模板', '静态资源', '配置', 'core', '采集层', '存储层',
        '报警层', '展示层', '智能层', '用户层', 'timeseries', 'tools',
    ]

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile', '--name', 'SCADA', '--console', '--noconfirm',
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

    exe = PROJECT_ROOT / 'dist' / 'SCADA.exe'
    if result.returncode == 0 and exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        dist = PROJECT_ROOT / 'dist'
        for d in ['data', 'logs', 'exports']:
            (dist / d).mkdir(exist_ok=True)
        if not (dist / '.env').exists() and (PROJECT_ROOT / '.env.example').exists():
            shutil.copy2(PROJECT_ROOT / '.env.example', dist / '.env')

        print(f"\n{'=' * 50}")
        print(f"  打包成功: {exe}")
        print(f"  大小: {size_mb:.1f} MB")
        print(f"  双击 SCADA.exe 自动开浏览器")
        print(f"{'=' * 50}")
    else:
        print("\n打包失败")


if __name__ == '__main__':
    build()
