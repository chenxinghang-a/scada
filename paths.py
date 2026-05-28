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
from pathlib import Path

# 项目根目录（自动检测，不依赖 __file__ 的调用位置）
PROJECT_ROOT = Path(__file__).resolve().parent

# 常用子目录
DATA_DIR = PROJECT_ROOT / 'data'
CONFIG_DIR = PROJECT_ROOT / '配置'
LOG_DIR = PROJECT_ROOT / 'logs'
EXPORT_DIR = PROJECT_ROOT / 'exports'

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
