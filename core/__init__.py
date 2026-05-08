"""
核心抽象层
提供依赖注入、模块注册表、配置管理、事件总线等基础设施
"""

from .di_container import DIContainer
from .module_registry import ModuleRegistry
from .config_manager import ConfigManager
from .event_bus import EventBus
from .health_checker import HealthChecker

__all__ = ['DIContainer', 'ModuleRegistry', 'ConfigManager', 'EventBus', 'HealthChecker']
