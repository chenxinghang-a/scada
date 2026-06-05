"""
核心抽象层
提供依赖注入、模块注册表、配置管理、事件总线等基础设施
"""

from .di_container import DIContainer
from .module_registry import ModuleRegistry
from .config_manager import ConfigManager
from .event_bus import EventBus
from .health_checker import HealthChecker
from .connection_pool import ConnectionPool, PooledConnection
from .circuit_breaker import CircuitBreaker, CircuitBreakerManager, circuit_breaker_manager
from .dynamic_rate_limiter import DynamicRateLimiter, dynamic_rate_limiter
from .degradation_manager import DegradationManager, DegradationLevel, degradation_manager
from .fault_injection import FaultInjector, FaultType, FaultSeverity, FaultScenarios, fault_injector
from .chaos_engineering import ChaosEngine, ExperimentState, chaos_engine

__all__ = [
    'DIContainer', 'ModuleRegistry', 'ConfigManager', 'EventBus',
    'HealthChecker', 'ConnectionPool', 'PooledConnection',
    'CircuitBreaker', 'CircuitBreakerManager', 'circuit_breaker_manager',
    'DynamicRateLimiter', 'dynamic_rate_limiter',
    'DegradationManager', 'DegradationLevel', 'degradation_manager',
    'FaultInjector', 'FaultType', 'FaultSeverity', 'FaultScenarios', 'fault_injector',
    'ChaosEngine', 'ExperimentState', 'chaos_engine',
]
