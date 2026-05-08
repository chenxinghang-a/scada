"""
模块注册表
管理模块的注册、初始化、状态和生命周期
"""

import logging
from typing import Any, Callable, Dict, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class ModuleStatus(Enum):
    """模块状态枚举"""
    REGISTERED = "registered"      # 已注册，未初始化
    INITIALIZING = "initializing"  # 正在初始化
    INITIALIZED = "initialized"    # 已初始化，可用
    RUNNING = "running"            # 运行中
    PAUSED = "paused"              # 已暂停
    ERROR = "error"                # 出错
    DISABLED = "disabled"          # 已禁用
    UNAVAILABLE = "unavailable"    # 不可用（如未连接）


class ModuleInfo:
    """模块信息"""
    def __init__(self, name: str, module_class: type, config: Dict[str, Any] = None):
        self.name = name
        self.module_class = module_class
        self.config = config or {}
        self.instance = None
        self.status = ModuleStatus.REGISTERED
        self.error = None
        self.dependencies = []
        self.metadata = {}
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'name': self.name,
            'class': self.module_class.__name__,
            'status': self.status.value,
            'error': str(self.error) if self.error else None,
            'has_instance': self.instance is not None,
            'dependencies': self.dependencies,
            'metadata': self.metadata
        }


class ModuleRegistry:
    """
    模块注册表
    
    管理所有模块的注册、初始化、状态监控和生命周期
    """
    
    _modules: Dict[str, ModuleInfo] = {}
    _initialization_order: List[str] = []
    
    @classmethod
    def register(cls, name: str, module_class: type, 
                 config: Dict[str, Any] = None, dependencies: List[str] = None):
        """
        注册模块
        
        Args:
            name: 模块名称
            module_class: 模块类
            config: 模块配置
            dependencies: 依赖的其他模块名称列表
        """
        if name in cls._modules:
            logger.warning(f"模块 '{name}' 已存在，将被覆盖")
        
        module_info = ModuleInfo(name, module_class, config)
        module_info.dependencies = dependencies or []
        cls._modules[name] = module_info
        
        logger.info(f"注册模块: {name} ({module_class.__name__})")
    
    @classmethod
    def initialize(cls, name: str, **kwargs) -> bool:
        """
        初始化模块
        
        Args:
            name: 模块名称
            **kwargs: 传递给模块构造函数的额外参数
            
        Returns:
            是否初始化成功
        """
        module_info = cls._modules.get(name)
        if not module_info:
            logger.error(f"模块 '{name}' 未注册")
            return False
        
        if module_info.status == ModuleStatus.INITIALIZED:
            logger.warning(f"模块 '{name}' 已初始化")
            return True
        
        # 检查依赖
        for dep_name in module_info.dependencies:
            dep_info = cls._modules.get(dep_name)
            if not dep_info or dep_info.status != ModuleStatus.INITIALIZED:
                logger.error(f"模块 '{name}' 的依赖 '{dep_name}' 未初始化")
                module_info.status = ModuleStatus.ERROR
                module_info.error = f"依赖 '{dep_name}' 未初始化"
                return False
        
        # 初始化模块
        module_info.status = ModuleStatus.INITIALIZING
        try:
            # 合并配置和额外参数
            init_config = {**module_info.config, **kwargs}
            module_info.instance = module_info.module_class(**init_config)
            module_info.status = ModuleStatus.INITIALIZED
            cls._initialization_order.append(name)
            logger.info(f"模块 '{name}' 初始化成功")
            return True
        except Exception as e:
            module_info.status = ModuleStatus.ERROR
            module_info.error = e
            logger.error(f"模块 '{name}' 初始化失败: {e}")
            return False
    
    @classmethod
    def initialize_all(cls) -> Dict[str, bool]:
        """
        按依赖顺序初始化所有模块
        
        Returns:
            模块名称到初始化结果的映射
        """
        results = {}
        
        # 拓扑排序
        sorted_modules = cls._topological_sort()
        
        for name in sorted_modules:
            results[name] = cls.initialize(name)
        
        return results
    
    @classmethod
    def _topological_sort(cls) -> List[str]:
        """
        拓扑排序，确保依赖先于被依赖者初始化
        
        Returns:
            排序后的模块名称列表
        """
        visited = set()
        result = []
        
        def dfs(name):
            if name in visited:
                return
            visited.add(name)
            
            module_info = cls._modules.get(name)
            if module_info:
                for dep in module_info.dependencies:
                    dfs(dep)
            
            result.append(name)
        
        for name in cls._modules:
            dfs(name)
        
        return result
    
    @classmethod
    def get_instance(cls, name: str) -> Any:
        """
        获取模块实例
        
        Args:
            name: 模块名称
            
        Returns:
            模块实例
        """
        module_info = cls._modules.get(name)
        if not module_info:
            raise KeyError(f"模块 '{name}' 未注册")
        
        if module_info.status != ModuleStatus.INITIALIZED:
            raise RuntimeError(f"模块 '{name}' 未初始化 (状态: {module_info.status.value})")
        
        return module_info.instance
    
    @classmethod
    def get_status(cls, name: str = None) -> Dict[str, Any]:
        """
        获取模块状态
        
        Args:
            name: 模块名称（None则返回所有模块状态）
            
        Returns:
            模块状态信息
        """
        if name:
            module_info = cls._modules.get(name)
            if not module_info:
                return {'status': 'not_found'}
            return module_info.to_dict()
        
        return {name: info.to_dict() for name, info in cls._modules.items()}
    
    @classmethod
    def set_status(cls, name: str, status: ModuleStatus, error: Exception = None):
        """
        设置模块状态
        
        Args:
            name: 模块名称
            status: 新状态
            error: 错误信息（如果状态为ERROR）
        """
        module_info = cls._modules.get(name)
        if module_info:
            module_info.status = status
            module_info.error = error
            logger.debug(f"模块 '{name}' 状态变更为: {status.value}")
    
    @classmethod
    def disable(cls, name: str):
        """
        禁用模块
        
        Args:
            name: 模块名称
        """
        cls.set_status(name, ModuleStatus.DISABLED)
        logger.info(f"模块 '{name}' 已禁用")
    
    @classmethod
    def enable(cls, name: str):
        """
        启用模块
        
        Args:
            name: 模块名称
        """
        module_info = cls._modules.get(name)
        if module_info and module_info.status == ModuleStatus.DISABLED:
            module_info.status = ModuleStatus.REGISTERED
            logger.info(f"模块 '{name}' 已启用")
    
    @classmethod
    def get_available_modules(cls) -> List[str]:
        """
        获取所有可用模块
        
        Returns:
            可用模块名称列表
        """
        return [
            name for name, info in cls._modules.items()
            if info.status in (ModuleStatus.INITIALIZED, ModuleStatus.RUNNING)
        ]
    
    @classmethod
    def get_unavailable_modules(cls) -> List[str]:
        """
        获取所有不可用模块
        
        Returns:
            不可用模块名称列表
        """
        return [
            name for name, info in cls._modules.items()
            if info.status in (ModuleStatus.ERROR, ModuleStatus.DISABLED, ModuleStatus.UNAVAILABLE)
        ]
    
    @classmethod
    def start(cls, name: str) -> bool:
        """
        启动模块
        
        Args:
            name: 模块名称
            
        Returns:
            是否启动成功
        """
        module_info = cls._modules.get(name)
        if not module_info:
            logger.error(f"模块 '{name}' 未注册")
            return False
        
        if module_info.status == ModuleStatus.RUNNING:
            logger.warning(f"模块 '{name}' 已在运行")
            return True
        
        if module_info.status not in (ModuleStatus.INITIALIZED, ModuleStatus.PAUSED):
            logger.error(f"模块 '{name}' 无法启动，当前状态: {module_info.status.value}")
            return False
        
        try:
            # 调用模块的start方法（如果存在）
            if hasattr(module_info.instance, 'start'):
                module_info.instance.start()
            
            module_info.status = ModuleStatus.RUNNING
            logger.info(f"模块 '{name}' 已启动")
            return True
        except Exception as e:
            module_info.status = ModuleStatus.ERROR
            module_info.error = e
            logger.error(f"模块 '{name}' 启动失败: {e}")
            return False
    
    @classmethod
    def stop(cls, name: str) -> bool:
        """
        停止模块
        
        Args:
            name: 模块名称
            
        Returns:
            是否停止成功
        """
        module_info = cls._modules.get(name)
        if not module_info:
            logger.error(f"模块 '{name}' 未注册")
            return False
        
        if module_info.status not in (ModuleStatus.RUNNING, ModuleStatus.PAUSED):
            logger.warning(f"模块 '{name}' 未在运行，当前状态: {module_info.status.value}")
            return True
        
        try:
            # 调用模块的stop方法（如果存在）
            if hasattr(module_info.instance, 'stop'):
                module_info.instance.stop()
            
            module_info.status = ModuleStatus.INITIALIZED
            logger.info(f"模块 '{name}' 已停止")
            return True
        except Exception as e:
            module_info.status = ModuleStatus.ERROR
            module_info.error = e
            logger.error(f"模块 '{name}' 停止失败: {e}")
            return False
    
    @classmethod
    def pause(cls, name: str) -> bool:
        """
        暂停模块
        
        Args:
            name: 模块名称
            
        Returns:
            是否暂停成功
        """
        module_info = cls._modules.get(name)
        if not module_info:
            logger.error(f"模块 '{name}' 未注册")
            return False
        
        if module_info.status != ModuleStatus.RUNNING:
            logger.warning(f"模块 '{name}' 未在运行，无法暂停")
            return False
        
        try:
            # 调用模块的pause方法（如果存在）
            if hasattr(module_info.instance, 'pause'):
                module_info.instance.pause()
            
            module_info.status = ModuleStatus.PAUSED
            logger.info(f"模块 '{name}' 已暂停")
            return True
        except Exception as e:
            module_info.status = ModuleStatus.ERROR
            module_info.error = e
            logger.error(f"模块 '{name}' 暂停失败: {e}")
            return False
    
    @classmethod
    def resume(cls, name: str) -> bool:
        """
        恢复模块
        
        Args:
            name: 模块名称
            
        Returns:
            是否恢复成功
        """
        module_info = cls._modules.get(name)
        if not module_info:
            logger.error(f"模块 '{name}' 未注册")
            return False
        
        if module_info.status != ModuleStatus.PAUSED:
            logger.warning(f"模块 '{name}' 未暂停，无法恢复")
            return False
        
        try:
            # 调用模块的resume方法（如果存在）
            if hasattr(module_info.instance, 'resume'):
                module_info.instance.resume()
            
            module_info.status = ModuleStatus.RUNNING
            logger.info(f"模块 '{name}' 已恢复")
            return True
        except Exception as e:
            module_info.status = ModuleStatus.ERROR
            module_info.error = e
            logger.error(f"模块 '{name}' 恢复失败: {e}")
            return False
    
    @classmethod
    def restart(cls, name: str) -> bool:
        """
        重启模块
        
        Args:
            name: 模块名称
            
        Returns:
            是否重启成功
        """
        module_info = cls._modules.get(name)
        if not module_info:
            logger.error(f"模块 '{name}' 未注册")
            return False
        
        # 先停止
        if module_info.status in (ModuleStatus.RUNNING, ModuleStatus.PAUSED):
            if not cls.stop(name):
                return False
        
        # 重新初始化
        module_info.instance = None
        module_info.status = ModuleStatus.REGISTERED
        
        # 重新初始化
        if not cls.initialize(name):
            return False
        
        # 启动
        return cls.start(name)
    
    @classmethod
    def get_lifecycle_info(cls, name: str = None) -> Dict[str, Any]:
        """
        获取模块生命周期信息
        
        Args:
            name: 模块名称（None则返回所有模块）
            
        Returns:
            生命周期信息
        """
        if name:
            module_info = cls._modules.get(name)
            if not module_info:
                return {'status': 'not_found'}
            
            return {
                'name': name,
                'status': module_info.status.value,
                'has_instance': module_info.instance is not None,
                'has_start_method': hasattr(module_info.instance, 'start') if module_info.instance else False,
                'has_stop_method': hasattr(module_info.instance, 'stop') if module_info.instance else False,
                'has_pause_method': hasattr(module_info.instance, 'pause') if module_info.instance else False,
                'has_resume_method': hasattr(module_info.instance, 'resume') if module_info.instance else False,
                'dependencies': module_info.dependencies,
                'error': str(module_info.error) if module_info.error else None
            }
        
        return {name: cls.get_lifecycle_info(name) for name in cls._modules}
    
    @classmethod
    def clear(cls):
        """清除所有模块注册"""
        # 先停止所有运行中的模块
        for name, info in cls._modules.items():
            if info.status in (ModuleStatus.RUNNING, ModuleStatus.PAUSED):
                cls.stop(name)
        
        cls._modules.clear()
        cls._initialization_order.clear()
        logger.debug("清除所有模块注册")
