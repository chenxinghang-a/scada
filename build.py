"""
打包脚本：将 SCADA 系统打包为桌面 exe
用法：python build.py
"""

import subprocess
import sys
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent


def build():
    print("=" * 50)
    print("  SCADA 桌面应用打包")
    print("=" * 50)

    # 安装依赖
    print("\n[1/4] 安装打包依赖...")
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'pyinstaller', 'pywebview', '-q'])

    # 清理
    for d in ['dist', 'build']:
        p = PROJECT_ROOT / d
        if p.exists():
            shutil.rmtree(p)
    spec = PROJECT_ROOT / 'SCADA.spec'
    if spec.exists():
        spec.unlink()

    # 打包
    print("\n[2/4] PyInstaller 打包中...")

    # 收集所有需要的数据目录
    data_dirs = [
        ('模板', '模板'),
        ('静态资源', '静态资源'),
        ('配置', '配置'),
        ('core', 'core'),
        ('采集层', '采集层'),
        ('存储层', '存储层'),
        ('报警层', '报警层'),
        ('展示层', '展示层'),
        ('智能层', '智能层'),
        ('用户层', '用户层'),
        ('timeseries', 'timeseries'),
        ('tools', 'tools'),
    ]

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',
        '--name', 'SCADA',
        '--windowed',           # 无控制台窗口（桌面应用）
        '--noconfirm',
    ]

    # 添加数据目录
    for src, dst in data_dirs:
        src_path = PROJECT_ROOT / src
        if src_path.exists():
            cmd.extend(['--add-data', f'{src_path};{dst}'])

    # 添加单文件模块
    for f in ['paths.py', 'config.py']:
        fp = PROJECT_ROOT / f
        if fp.exists():
            cmd.extend(['--add-data', f'{fp};.'])

    # Hidden imports
    hidden_imports = [
        'flask', 'flask_socketio', 'socketio', 'engineio',
        'pymodbus', 'asyncua', 'paho.mqtt', 'jwt', 'bcrypt',
        'pandas', 'numpy', 'yaml', 'loguru', 'apscheduler',
        'openpyxl', 'dotenv', 'requests', 'webview',
        'webview.platforms.edgechromium', 'webview.platforms.winforms',
    ]
    for imp in hidden_imports:
        cmd.extend(['--hidden-import', imp])

    # 排除不需要的模块
    for exc in ['tkinter', 'matplotlib', 'PIL', 'scipy', 'sklearn']:
        cmd.extend(['--exclude-module', exc])

    cmd.append('launcher.py')

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))

    # 检查结果
    exe_path = PROJECT_ROOT / 'dist' / 'SCADA.exe'
    if result.returncode == 0 and exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)

        # 复制运行时文件到 dist 目录
        print("\n[3/4] 复制运行时文件...")
        dist = PROJECT_ROOT / 'dist'
        for subdir in ['data', 'logs', 'exports']:
            (dist / subdir).mkdir(exist_ok=True)

        if not (dist / '.env').exists():
            env_example = PROJECT_ROOT / '.env.example'
            if env_example.exists():
                shutil.copy2(env_example, dist / '.env')

        print("\n[4/4] 完成！")
        print(f"\n{'=' * 50}")
        print(f"  输出: {exe_path}")
        print(f"  大小: {size_mb:.1f} MB")
        print(f"{'=' * 50}")
        print(f"\n使用方式:")
        print(f"  双击 SCADA.exe 启动（自动弹出窗口）")
        print(f"  命令行: SCADA.exe --real")
    else:
        print(f"\n[错误] 打包失败")
        print("如果缺少依赖，先运行: pip install pywebview pyinstaller")


if __name__ == '__main__':
    build()
