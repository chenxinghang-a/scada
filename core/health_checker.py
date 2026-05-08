"""
健康检查器
监控各模块的健康状态
"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class HealthStatus:
    """健康状态"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class HealthCheck:
    """健康检查项"""
    def __init__(self, name: str, check_func: Callable[[], Dict[str, Any]], 
                 interval: int = 60, timeout: int = 10):
        """
        初始化健康检查
        
        Args:
            name: 检查名称
            check_func: 检查函数，返回 {'status': str, 'message': str, 'details': dict}
            interval: 检查间隔（秒）
            timeout: 超时时间（秒）
        """
        self.name = name
        self.check_func = check_func
        self.interval = interval
        self.timeout = timeout
        self.last_check = None
        self.last_result = None
        self.history = []
        self.max_history = 100
    
    def run(self) -> Dict[str, Any]:
        """
        运行健康检查
        
        Returns:
            检查结果
        """
        start_time = time.time()
        
        try:
            result = self.check_func()
            duration = time.time() - start_time
            
            # 确保结果包含必要字段
            if 'status' not in result:
                result['status'] = HealthStatus.UNKNOWN
            if 'message' not in result:
                result['message'] = ''
            if 'details' not in result:
                result['details'] = {}
            
            result['duration'] = duration
            result['timestamp'] = datetime.now().isoformat()
            
            # 更新状态
            self.last_check = datetime.now()
            self.last_result = result
            
            # 记录历史
            self.history.append(result)
            if len(self.history) > self.max_history:
                self.history.pop(0)
            
            return result
        except Exception as e:
            duration = time.time() - start_time
            result = {
                'status': HealthStatus.UNHEALTHY,
                'message': str(e),
                'details': {'error': type(e).__name__},
                'duration': duration,
                'timestamp': datetime.now().isoformat()
            }
            
            self.last_check = datetime.now()
            self.last_result = result
            self.history.append(result)
            
            return result
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'name': self.name,
            'interval': self.interval,
            'timeout': self.timeout,
            'last_check': self.last_check.isoformat() if self.last_check else None,
            'last_result': self.last_result,
            'history_count': len(self.history)
        }


class HealthChecker:
    """
    健康检查器
    
    管理和运行所有健康检查
    """
    
    _checks: Dict[str, HealthCheck] = {}
    _global_status = HealthStatus.UNKNOWN
    
    @classmethod
    def register(cls, name: str, check_func: Callable[[], Dict[str, Any]], 
                 interval: int = 60, timeout: int = 10):
        """
        注册健康检查
        
        Args:
            name: 检查名称
            check_func: 检查函数
            interval: 检查间隔（秒）
            timeout: 超时时间（秒）
        """
        cls._checks[name] = HealthCheck(name, check_func, interval, timeout)
        logger.info(f"注册健康检查: {name}")
    
    @classmethod
    def unregister(cls, name: str):
        """
        取消注册健康检查
        
        Args:
            name: 检查名称
        """
        if name in cls._checks:
            del cls._checks[name]
            logger.info(f"取消注册健康检查: {name}")
    
    @classmethod
    def check(cls, name: str = None) -> Dict[str, Any]:
        """
        运行健康检查
        
        Args:
            name: 检查名称（None则运行所有）
            
        Returns:
            检查结果
        """
        if name:
            check = cls._checks.get(name)
            if not check:
                return {'status': HealthStatus.UNKNOWN, 'message': f'检查 {name} 未注册'}
            return check.run()
        
        # 运行所有检查
        results = {}
        overall_status = HealthStatus.HEALTHY
        
        for check_name, check in cls._checks.items():
            result = check.run()
            results[check_name] = result
            
            # 更新整体状态
            if result['status'] == HealthStatus.UNHEALTHY:
                overall_status = HealthStatus.UNHEALTHY
            elif result['status'] == HealthStatus.DEGRADED and overall_status != HealthStatus.UNHEALTHY:
                overall_status = HealthStatus.DEGRADED
        
        cls._global_status = overall_status
        
        return {
            'status': overall_status,
            'checks': results,
            'timestamp': datetime.now().isoformat()
        }
    
    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        """
        获取健康状态概览
        
        Returns:
            健康状态信息
        """
        checks_status = {}
        for name, check in cls._checks.items():
            checks_status[name] = {
                'status': check.last_result['status'] if check.last_result else HealthStatus.UNKNOWN,
                'last_check': check.last_check.isoformat() if check.last_check else None
            }
        
        return {
            'global_status': cls._global_status,
            'checks': checks_status,
            'total_checks': len(cls._checks)
        }
    
    @classmethod
    def get_history(cls, name: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取检查历史
        
        Args:
            name: 检查名称
            limit: 返回数量限制
            
        Returns:
            检查历史列表
        """
        check = cls._checks.get(name)
        if not check:
            return []
        
        return check.history[-limit:]
    
    @classmethod
    def clear(cls):
        """清除所有检查"""
        cls._checks.clear()
        cls._global_status = HealthStatus.UNKNOWN
        logger.debug("清除所有健康检查")
