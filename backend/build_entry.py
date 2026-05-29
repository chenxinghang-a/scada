"""
PyInstaller 打包入口
处理 frozen 模式下的路径问题，然后启动原 run.py
"""
import sys
import os
from pathlib import Path

# PyInstaller frozen 模式
if getattr(sys, 'frozen', False):
    # _internal 目录（PyInstaller 把 --add-data 的资源和依赖都放在这里）
    internal_dir = Path(sys.executable).parent / '_internal'
    # 工作目录设为 _internal，这样原始代码的相对路径（配置/、模板/等）都能找到
    os.chdir(internal_dir)
    sys.path.insert(0, str(internal_dir))
    # 创建运行时目录
    (internal_dir / 'logs').mkdir(exist_ok=True)
    (internal_dir / 'data').mkdir(exist_ok=True)
    (internal_dir / 'exports').mkdir(exist_ok=True)

# 启动原始 run.py
from run import main
main()
