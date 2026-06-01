"""
路径管理模块
============
统一管理项目路径，避免到处 sys.path.insert。

用法:
    # run.py 入口处调用一次
    import paths
    paths.setup()

    # 其他模块直接用
    from paths import PROJECT_ROOT, DATA_DIR, CONFIG_DIR, LOG_DIR
"""

import sys
import os
from pathlib import Path


def _get_project_root():
    """获取项目根目录，兼容PyInstaller打包"""
    if getattr(sys, 'frozen', False):
        # PyInstaller onefile模式：exe所在目录
        exe_dir = Path(sys.executable).parent
        # 检查_internal目录是否存在（PyInstaller打包后的标准结构）
        if (exe_dir / '_internal').exists():
            return exe_dir
        # 如果没有_internal，检查配置目录是否直接在exe目录下
        if (exe_dir / '配置').exists():
            return exe_dir
        # 向上一级查找（可能exe在子目录中）
        parent_dir = exe_dir.parent
        if (parent_dir / '_internal').exists():
            return parent_dir
        if (parent_dir / '配置').exists():
            return parent_dir
        # 默认返回exe目录
        return exe_dir
    # 开发模式：paths.py所在目录
    return Path(__file__).resolve().parent


# 项目根目录
PROJECT_ROOT = _get_project_root()

# 根据目录结构确定数据目录
if (PROJECT_ROOT / '_internal').exists():
    # PyInstaller打包后的标准结构
    _BASE = PROJECT_ROOT / '_internal'
elif (PROJECT_ROOT / '配置').exists():
    # 配置目录直接在项目根目录下
    _BASE = PROJECT_ROOT
else:
    _BASE = PROJECT_ROOT

DATA_DIR = _BASE / 'data'
CONFIG_DIR = _BASE / '配置'
LOG_DIR = _BASE / 'logs'
EXPORT_DIR = _BASE / 'exports'

# 数据库路径（按模式区分）
DB_PATHS = {
    'simulated': DATA_DIR / 'scada_simulated.db',
    'real': DATA_DIR / 'scada_real.db',
    'simulator': DATA_DIR / 'scada_simulator.db',
    'default': DATA_DIR / 'scada.db',
}


def setup():
    """
    初始化项目路径（在入口脚本最开始调用一次）

    - 将项目根目录加入 sys.path
    - 确保必要目录存在
    """
    root_str = str(PROJECT_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    for d in [DATA_DIR, LOG_DIR, EXPORT_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def get_db_path(mode: str = 'simulated') -> str:
    """获取数据库路径"""
    return str(DB_PATHS.get(mode, DB_PATHS['default']))


def get_config_path(filename: str) -> str:
    """获取配置文件路径"""
    return str(CONFIG_DIR / filename)
