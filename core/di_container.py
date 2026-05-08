"""
依赖注入容器
实现服务注册、解析和生命周期管理
"""

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class DIContainer:
    """
    依赖注入容器
    
    支持三种生命周期：
    1. transient - 每次解析都创建新实例
    2. singleton - 单例模式，全局共享一个实例
    3. scoped - 作用域内共享实例（如每个请求）
    """
    
    _services: Dict[str, Dict[str, Any]] = {}
    _singletons: Dict[str, Any] = {}
    _scoped: Dict[str, Dict[str, Any]] = {}
    
    @classmethod
    def register(cls, name: str, factory: Callable[..., Any], 
                 lifecycle: str = 'transient', dependencies: list = None):
        """
        注册服务
        
        Args:
            name: 服务名称
            factory: 工厂函数或类
            lifecycle: 生命周期 ('transient', 'singleton', 'scoped')
            dependencies: 依赖的其他服务名称列表
        """
        if lifecycle not in ('transient', 'singleton', 'scoped'):
            raise ValueError(f"不支持的生命周期: {lifecycle}")
        
        cls._services[name] = {
            'factory': factory,
            'lifecycle': lifecycle,
            'dependencies': dependencies or []
        }
        logger.debug(f"注册服务: {name} (lifecycle={lifecycle})")
    
    @classmethod
    def register_instance(cls, name: str, instance: Any):
        """
        注册现有实例为单例
        
        Args:
            name: 服务名称
            instance: 服务实例
        """
        cls._singletons[name] = instance
        logger.debug(f"注册实例: {name}")
    
    @classmethod
    def resolve(cls, name: str, scope_id: str = None) -> Any:
        """
        解析服务
        
        Args:
            name: 服务名称
            scope_id: 作用域ID（用于scoped生命周期）
            
        Returns:
            服务实例
        """
        # 检查单例
        if name in cls._singletons:
            return cls._singletons[name]
        
        # 检查服务注册
        service = cls._services.get(name)
        if not service:
            raise KeyError(f"服务 '{name}' 未注册")
        
        # 解析依赖
        dependencies = []
        for dep_name in service['dependencies']:
            dep = cls.resolve(dep_name, scope_id)
            dependencies.append(dep)
        
        # 根据生命周期创建实例
        lifecycle = service['lifecycle']
        
        if lifecycle == 'singleton':
            if name not in cls._singletons:
                cls._singletons[name] = service['factory'](*dependencies)
            return cls._singletons[name]
        
        elif lifecycle == 'scoped':
            if scope_id is None:
                raise ValueError("scoped生命周期需要scope_id")
            
            if scope_id not in cls._scoped:
                cls._scoped[scope_id] = {}
            
            if name not in cls._scoped[scope_id]:
                cls._scoped[scope_id][name] = service['factory'](*dependencies)
            
            return cls._scoped[scope_id][name]
        
        else:  # transient
            return service['factory'](*dependencies)
    
    @classmethod
    def clear_scope(cls, scope_id: str):
        """
        清除作用域内的所有实例
        
        Args:
            scope_id: 作用域ID
        """
        if scope_id in cls._scoped:
            del cls._scoped[scope_id]
            logger.debug(f"清除作用域: {scope_id}")
    
    @classmethod
    def clear_all(cls):
        """清除所有注册和实例"""
        cls._services.clear()
        cls._singletons.clear()
        cls._scoped.clear()
        logger.debug("清除所有服务注册")
    
    @classmethod
    def get_registered_services(cls) -> Dict[str, str]:
        """
        获取所有已注册的服务
        
        Returns:
            服务名称到生命周期的映射
        """
        result = {}
        for name, service in cls._services.items():
            result[name] = service['lifecycle']
        return result
    
    @classmethod
    def is_registered(cls, name: str) -> bool:
        """
        检查服务是否已注册
        
        Args:
            name: 服务名称
            
        Returns:
            是否已注册
        """
        return name in cls._services or name in cls._singletons
