"""
配置管理器
统一管理YAML配置和Python配置类
"""

import logging
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ConfigManager:
    """
    配置管理器
    
    支持：
    1. YAML配置文件加载
    2. 环境变量覆盖
    3. 配置验证
    4. 配置热更新
    """
    
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
        if config_path in cls._configs and not reload:
            return cls._configs[config_path]
        
        try:
            path = Path(config_path)
            if not path.exists():
                logger.warning(f"配置文件不存在: {config_path}")
                return {}
            
            with open(path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            
            # 缓存配置
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
        config = cls._configs.get(config_path, {})
        
        # 支持点号分隔的路径
        keys = key.split('.')
        value = config
        
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        
        return value if value is not None else default
    
    @classmethod
    def set(cls, config_path: str, key: str, value: Any):
        """
        设置配置值
        
        Args:
            config_path: 配置文件路径
            key: 配置键（支持点号分隔的路径）
            value: 配置值
        """
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
        if config_path not in cls._watchers:
            cls._watchers[config_path] = []
        
        cls._watchers[config_path].append(callback)
    
    @classmethod
    def _notify_watchers(cls, config_path: str, config: Dict[str, Any]):
        """通知配置观察者"""
        watchers = cls._watchers.get(config_path, [])
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
        return cls._configs.copy()
    
    @classmethod
    def clear(cls):
        """清除所有配置缓存"""
        cls._configs.clear()
        cls._watchers.clear()
        logger.debug("清除所有配置缓存")
