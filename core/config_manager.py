"""
配置管理器
统一管理YAML配置和Python配置类
"""

import logging
import os
import re
import threading
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MISSING = object()  # 区分"键不存在"和"键存在但值为None"

# 匹配 ${VAR} 或 ${VAR:-default} 形式的环境变量引用
_ENV_VAR_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}')


def expand_env_vars(value: Any, _source: str = '') -> Any:
    """
    递归展开配置值中的 ${VAR} / ${VAR:-default} 环境变量引用。

    - 环境变量已设置：替换为其值。
    - 未设置且写了 :-default：替换为 default。
    - 未设置且无默认值：保留原始占位符并打印警告（避免静默变成空串
      造成"看着能跑"的假配置，部署方必须能在日志中发现缺失）。
    """
    if isinstance(value, str):
        def _repl(match: 're.Match') -> str:
            var_name = match.group(1)
            default = match.group(2)
            env_value = os.environ.get(var_name)
            if env_value is not None:
                return env_value
            if default is not None:
                return default
            logger.warning(
                "配置项引用的环境变量 %s 未设置（%s），保留占位符 ${%s}。"
                "请在 .env 或部署环境中设置该变量。",
                var_name, _source or 'YAML配置', var_name,
            )
            return match.group(0)
        return _ENV_VAR_PATTERN.sub(_repl, value)
    if isinstance(value, dict):
        return {k: expand_env_vars(v, _source) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item, _source) for item in value]
    return value


class ConfigManager:
    """
    配置管理器

    支持：
    1. YAML配置文件加载
    2. 环境变量覆盖
    3. 配置验证
    4. 配置热更新
    """

    _lock = threading.Lock()
    _configs: Dict[str, Dict[str, Any]] = {}
    _watchers: Dict[str, list] = {}
    
    @classmethod
    def load_yaml(cls, config_path: str, reload: bool = False) -> Dict[str, Any]:
        """
        加载YAML配置文件
        
        Args:
            config_path: 配置文件路径
            reload: 是否强制重新加载
            
        Returns:
            配置字典
        """
        config_path = str(config_path)

        # 检查缓存
        with cls._lock:
            if config_path in cls._configs and not reload:
                return cls._configs[config_path]

        try:
            path = Path(config_path)
            if not path.exists():
                logger.warning(f"配置文件不存在: {config_path}")
                return {}

            with open(path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            # 展开 ${VAR} / ${VAR:-default} 环境变量引用
            config = expand_env_vars(config, _source=config_path)

            # 缓存配置
            with cls._lock:
                cls._configs[config_path] = config
            logger.info(f"加载配置文件: {config_path}")

            # 通知观察者
            cls._notify_watchers(config_path, config)

            return config
        except Exception as e:
            logger.error(f"加载配置文件失败: {config_path}, 错误: {e}")
            return {}
    
    @classmethod
    def save_yaml(cls, config_path: str, config: Dict[str, Any]):
        """
        保存配置到YAML文件
        
        Args:
            config_path: 配置文件路径
            config: 配置字典
        """
        try:
            path = Path(config_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

            # 更新缓存
            with cls._lock:
                cls._configs[config_path] = config
            logger.info(f"保存配置文件: {config_path}")

            # 通知观察者
            cls._notify_watchers(config_path, config)
        except Exception as e:
            logger.error(f"保存配置文件失败: {config_path}, 错误: {e}")
    
    @classmethod
    def get(cls, config_path: str, key: str, default: Any = None) -> Any:
        """
        获取配置值
        
        Args:
            config_path: 配置文件路径
            key: 配置键（支持点号分隔的路径，如 'database.host'）
            default: 默认值
            
        Returns:
            配置值
        """
        with cls._lock:
            config = cls._configs.get(config_path, {}).copy()

        # 支持点号分隔的路径
        keys = key.split('.')
        value = config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, _MISSING)
            else:
                return default

        return default if value is _MISSING else value
    
    @classmethod
    def set(cls, config_path: str, key: str, value: Any):
        """
        设置配置值
        
        Args:
            config_path: 配置文件路径
            key: 配置键（支持点号分隔的路径）
            value: 配置值
        """
        with cls._lock:
            if config_path not in cls._configs:
                cls._configs[config_path] = {}

            config = cls._configs[config_path]
            keys = key.split('.')

            # 创建嵌套字典
            for k in keys[:-1]:
                if k not in config:
                    config[k] = {}
                config = config[k]

            config[keys[-1]] = value
    
    @classmethod
    def watch(cls, config_path: str, callback: callable):
        """
        监听配置变化
        
        Args:
            config_path: 配置文件路径
            callback: 回调函数，参数为 (config_path, config)
        """
        with cls._lock:
            if config_path not in cls._watchers:
                cls._watchers[config_path] = []

            cls._watchers[config_path].append(callback)
    
    @classmethod
    def _notify_watchers(cls, config_path: str, config: Dict[str, Any]):
        """通知配置观察者"""
        with cls._lock:
            watchers = list(cls._watchers.get(config_path, []))
        for callback in watchers:
            try:
                callback(config_path, config)
            except Exception as e:
                logger.error(f"配置观察者回调失败: {e}")
    
    @classmethod
    def get_all_configs(cls) -> Dict[str, Dict[str, Any]]:
        """
        获取所有已加载的配置

        Returns:
            配置路径到配置字典的映射
        """
        with cls._lock:
            return cls._configs.copy()

    @classmethod
    def clear(cls):
        """清除所有配置缓存"""
        with cls._lock:
            cls._configs.clear()
            cls._watchers.clear()
        logger.debug("清除所有配置缓存")
