"""
API公共工具函数
"""

from typing import Any
import yaml
import logging
from pathlib import Path
from flask import current_app

logger = logging.getLogger(__name__)


def get_auth_manager() -> Any:
    """获取认证管理器"""
    return current_app.auth_manager


def load_yaml_config(config_path: str) -> dict[str, Any]:
    """加载YAML配置文件"""
    path = Path(config_path)
    if not path.exists():
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def save_yaml_config(config_path: str, config: dict[str, Any]) -> bool:
    """保存YAML配置文件（原子写入：先写临时文件再 rename）"""
    try:
        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        tmp_path.replace(path)
        return True
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")
        return False
