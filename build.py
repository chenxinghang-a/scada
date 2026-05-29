"""
打包脚本：将 SCADA 系统打包为单个 exe
用法：python build.py
"""

import subprocess
import sys
import os
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
DIST_DIR = PROJECT_ROOT / 'dist'
BUILD_DIR = PROJECT_ROOT / 'build'

def build():
    print("=" * 50)
    print("  SCADA 系统打包 (PyInstaller)")
    print("=" * 50)

    # 清理旧构建
    for d in [DIST_DIR, BUILD_DIR]:
        if d.exists():
            shutil.rmtree(d)
            print(f"[清理] {d}")

    # PyInstaller 参数
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',                        # 单文件
        '--name', 'SCADA',                  # exe 名称
        '--console',                        # 保留控制台（显示日志）
        '--add-data', f'模板;模板',           # Jinja2 模板
        '--add-data', f'静态资源;静态资源',    # CSS/JS/图片
        '--add-data', f'配置;配置',           # YAML 配置
        '--add-data', f'paths.py;.',         # 路径模块
        '--add-data', f'config.py;.',        # 配置模块
        '--add-data', f'core;core',          # 核心模块
        '--add-data', f'采集层;采集层',       # 协议客户端
        '--add-data', f'存储层;存储层',       # 数据库
        '--add-data', f'报警层;报警层',       # 报警系统
        '--add-data', f'展示层;展示层',       # Web 展示
        '--add-data', f'智能层;智能层',       # 智能分析
        '--add-data', f'用户层;用户层',       # 认证
        '--add-data', f'timeseries;timeseries',  # 时序数据库
        '--add-data', f'tools;tools',        # 工具
        '--hidden-import', 'flask',
        '--hidden-import', 'flask_socketio',
        '--hidden-import', 'socketio',
        '--hidden-import', 'engineio',
        '--hidden-import', 'pymodbus',
        '--hidden-import', 'asyncua',
        '--hidden-import', 'paho.mqtt',
        '--hidden-import', 'jwt',
        '--hidden-import', 'bcrypt',
        '--hidden-import', 'pandas',
        '--hidden-import', 'numpy',
        '--hidden-import', 'yaml',
        '--hidden-import', 'loguru',
        '--hidden-import', 'apscheduler',
        '--hidden-import', 'openpyxl',
        '--hidden-import', 'dotenv',
        '--hidden-import', 'requests',
        '--exclude-module', 'tkinter',
        '--exclude-module', 'matplotlib',
        '--exclude-module', 'PIL',
        'launcher.py',                      # 入口脚本
    ]

    print("\n[构建] 开始打包...")
    print(f"命令: {' '.join(cmd[:5])}...")

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))

    if result.returncode == 0:
        exe_path = DIST_DIR / 'SCADA.exe'
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            print(f"\n{'=' * 50}")
            print(f"  打包成功！")
            print(f"  输出: {exe_path}")
            print(f"  大小: {size_mb:.1f} MB")
            print(f"{'=' * 50}")
            print(f"\n使用方式:")
            print(f"  模拟模式: SCADA.exe")
            print(f"  真实模式: SCADA.exe --real")
            print(f"  模拟器:   SCADA.exe --simulator")
        else:
            print("[错误] exe 文件未生成")
    else:
        print(f"[错误] 打包失败，返回码: {result.returncode}")


if __name__ == '__main__':
    build()
