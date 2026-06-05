"""
故障注入测试框架
模拟各类故障场景，验证系统容错能力。

支持的故障类型:
  - 延迟注入（latency）
  - 异常注入（exception）
  - 超时注入（timeout）
  - 资源耗尽（resource_exhaustion）
  - 网络分区（network_partition）
  - 数据损坏（data_corruption）

使用示例:
    injector = FaultInjector()

    # 注入延迟故障
    injector.inject('database', FaultType.LATENCY, delay_ms=500, duration=60)

    # 注入异常故障
    injector.inject('device_comm', FaultType.EXCEPTION, exception=ConnectionError("模拟断连"))

    # 使用装饰器
    @injector.decorate('api', FaultType.LATENCY, delay_ms=200)
    def api_call():
        return fetch_data()
"""

import threading
import time
import random
import logging
from enum import Enum
from typing import Dict, Any, Optional, Callable, List, Set
from functools import wraps
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class FaultType(Enum):
    """故障类型"""
    LATENCY = "latency"                    # 延迟
    EXCEPTION = "exception"                # 异常
    TIMEOUT = "timeout"                    # 超时
    RESOURCE_EXHAUSTION = "resource_exhaustion"  # 资源耗尽
    NETWORK_PARTITION = "network_partition"      # 网络分区
    DATA_CORRUPTION = "data_corruption"          # 数据损坏
    SLOW_RESPONSE = "slow_response"              # 慢响应（渐进式延迟）
    INTERMITTENT = "intermittent"                # 间歇性故障


class FaultSeverity(Enum):
    """故障严重度"""
    LOW = "low"           # 轻微影响
    MEDIUM = "medium"     # 中等影响
    HIGH = "high"         # 严重影响
    CRITICAL = "critical" # 致命影响


class FaultInjection:
    """单个故障注入实例"""
    __slots__ = ('target', 'fault_type', 'severity', 'params',
                 'start_time', 'end_time', 'triggered_count',
                 'affected_calls', '_active', '_lock')

    def __init__(
        self,
        target: str,
        fault_type: FaultType,
        severity: FaultSeverity = FaultSeverity.MEDIUM,
        duration: float = 60.0,
        **params,
    ):
        self.target = target
        self.fault_type = fault_type
        self.severity = severity
        self.params = params
        self.start_time = time.time()
        self.end_time = self.start_time + duration
        self.triggered_count = 0
        self.affected_calls = 0
        self._active = True
        self._lock = threading.Lock()

    @property
    def is_active(self) -> bool:
        if not self._active:
            return False
        if time.time() > self.end_time:
            self._active = False
            return False
        return True

    def apply(self) -> Any:
        """应用故障效果"""
        if not self.is_active:
            return None

        with self._lock:
            self.triggered_count += 1

        if self.fault_type == FaultType.LATENCY:
            delay = self.params.get('delay_ms', 100) / 1000.0
            jitter = self.params.get('jitter_ms', 0) / 1000.0
            actual_delay = delay + random.uniform(0, jitter) if jitter else delay
            time.sleep(actual_delay)
            return None

        elif self.fault_type == FaultType.EXCEPTION:
            exc_class = self.params.get('exception_class', Exception)
            exc_msg = self.params.get('exception_message', '故障注入: 模拟异常')
            raise exc_class(exc_msg)

        elif self.fault_type == FaultType.TIMEOUT:
            timeout = self.params.get('timeout_seconds', 5.0)
            time.sleep(timeout + 0.1)
            raise TimeoutError(f'故障注入: 超时 ({timeout}s)')

        elif self.fault_type == FaultType.RESOURCE_EXHAUSTION:
            raise ResourceWarning('故障注入: 资源耗尽')

        elif self.fault_type == FaultType.NETWORK_PARTITION:
            raise ConnectionError('故障注入: 网络分区')

        elif self.fault_type == FaultType.DATA_CORRUPTION:
            return self.params.get('corrupted_value', None)

        elif self.fault_type == FaultType.SLOW_RESPONSE:
            base = self.params.get('base_delay_ms', 50) / 1000.0
            increment = self.params.get('increment_ms', 10) / 1000.0
            delay = base + (self.triggered_count * increment)
            time.sleep(min(delay, 10.0))
            return None

        elif self.fault_type == FaultType.INTERMITTENT:
            failure_rate = self.params.get('failure_rate', 0.3)
            if random.random() < failure_rate:
                raise ConnectionError('故障注入: 间歇性连接失败')
            return None

        return None

    def deactivate(self):
        """手动停用"""
        self._active = False

    def to_dict(self) -> Dict[str, Any]:
        """序列化"""
        return {
            'target': self.target,
            'fault_type': self.fault_type.value,
            'severity': self.severity.value,
            'active': self.is_active,
            'triggered_count': self.triggered_count,
            'affected_calls': self.affected_calls,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'remaining_seconds': max(0, self.end_time - time.time()),
            'params': {k: str(v) for k, v in self.params.items()},
        }


class FaultInjector:
    """
    故障注入管理器

    集中管理所有故障注入点，提供注入/恢复/查询接口。
    """

    def __init__(self):
        self._injections: Dict[str, FaultInjection] = {}
        self._history: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._global_enabled = True

    @property
    def enabled(self) -> bool:
        return self._global_enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._global_enabled = value
        logger.info("故障注入全局开关: %s", "启用" if value else "禁用")

    def inject(
        self,
        target: str,
        fault_type: FaultType,
        severity: FaultSeverity = FaultSeverity.MEDIUM,
        duration: float = 60.0,
        **params,
    ) -> FaultInjection:
        """
        注入故障

        Args:
            target: 故障目标标识（如 'database', 'device_comm', 'api'）
            fault_type: 故障类型
            severity: 严重度
            duration: 持续时间（秒）
            **params: 故障参数（delay_ms, exception_class, failure_rate 等）

        Returns:
            FaultInjection 实例
        """
        if not self._global_enabled:
            logger.warning("故障注入已禁用，忽略注入请求")
            return None

        injection = FaultInjection(target, fault_type, severity, duration, **params)

        with self._lock:
            # 停用同目标的旧注入
            if target in self._injections:
                self._injections[target].deactivate()
            self._injections[target] = injection
            self._history.append({
                'action': 'inject',
                'target': target,
                'fault_type': fault_type.value,
                'severity': severity.value,
                'duration': duration,
                'time': datetime.now().isoformat(),
            })

        logger.warning(
            "故障注入: target=%s, type=%s, severity=%s, duration=%ds",
            target, fault_type.value, severity.value, int(duration),
        )
        return injection

    def remove(self, target: str) -> bool:
        """移除故障注入"""
        with self._lock:
            injection = self._injections.pop(target, None)
            if injection:
                injection.deactivate()
                self._history.append({
                    'action': 'remove',
                    'target': target,
                    'time': datetime.now().isoformat(),
                })
                logger.info("故障注入已移除: %s", target)
                return True
            return False

    def check(self, target: str) -> Optional[FaultInjection]:
        """检查目标是否有活跃的故障注入"""
        with self._lock:
            injection = self._injections.get(target)
            if injection and injection.is_active:
                return injection
            elif injection:
                # 过期清理
                del self._injections[target]
            return None

    def apply(self, target: str) -> bool:
        """
        对目标应用故障（如有注入）

        Returns:
            True 如果故障已触发，False 如果无注入
        """
        injection = self.check(target)
        if injection:
            injection.affected_calls += 1
            injection.apply()
            return True
        return False

    def decorate(self, target: str, fault_type: FaultType, **params):
        """
        装饰器方式注入故障

        使用:
            @injector.decorate('api', FaultType.LATENCY, delay_ms=200, duration=30)
            def api_call():
                ...
        """
        injection = self.inject(target, fault_type, **params)

        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                inj = self.check(target)
                if inj:
                    inj.affected_calls += 1
                    inj.apply()
                return func(*args, **kwargs)
            return wrapper
        return decorator

    def clear_all(self):
        """清除所有故障注入"""
        with self._lock:
            for injection in self._injections.values():
                injection.deactivate()
            self._injections.clear()
            self._history.append({
                'action': 'clear_all',
                'time': datetime.now().isoformat(),
            })
        logger.info("所有故障注入已清除")

    def get_active(self) -> List[Dict[str, Any]]:
        """获取所有活跃的故障注入"""
        with self._lock:
            active = []
            expired = []
            for name, inj in self._injections.items():
                if inj.is_active:
                    active.append(inj.to_dict())
                else:
                    expired.append(name)
            # 清理过期
            for name in expired:
                del self._injections[name]
            return active

    def get_status(self) -> Dict[str, Any]:
        """获取故障注入器状态"""
        with self._lock:
            return {
                'global_enabled': self._global_enabled,
                'active_count': len([i for i in self._injections.values() if i.is_active]),
                'total_injected': len(self._history),
                'active_injections': self.get_active(),
                'recent_history': self._history[-20:],
            }


# ================================================================
# 预定义故障场景
# ================================================================

class FaultScenarios:
    """预定义的故障场景模板"""

    @staticmethod
    def database_slow(injector: FaultInjector, duration: float = 120):
        """数据库慢查询场景"""
        return injector.inject(
            'database', FaultType.LATENCY,
            severity=FaultSeverity.HIGH,
            duration=duration,
            delay_ms=2000,
            jitter_ms=500,
        )

    @staticmethod
    def database_down(injector: FaultInjector, duration: float = 60):
        """数据库不可用场景"""
        return injector.inject(
            'database', FaultType.EXCEPTION,
            severity=FaultSeverity.CRITICAL,
            duration=duration,
            exception_class=ConnectionError,
            exception_message='数据库连接失败',
        )

    @staticmethod
    def device_network_loss(injector: FaultInjector, duration: float = 180):
        """设备网络丢包场景"""
        return injector.inject(
            'device_comm', FaultType.INTERMITTENT,
            severity=FaultSeverity.MEDIUM,
            duration=duration,
            failure_rate=0.5,
        )

    @staticmethod
    def device_timeout(injector: FaultInjector, duration: float = 120):
        """设备响应超时场景"""
        return injector.inject(
            'device_comm', FaultType.TIMEOUT,
            severity=FaultSeverity.HIGH,
            duration=duration,
            timeout_seconds=10,
        )

    @staticmethod
    def api_overload(injector: FaultInjector, duration: float = 90):
        """API过载场景"""
        return injector.inject(
            'api', FaultType.SLOW_RESPONSE,
            severity=FaultSeverity.MEDIUM,
            duration=duration,
            base_delay_ms=100,
            increment_ms=50,
        )

    @staticmethod
    def websocket_partition(injector: FaultInjector, duration: float = 60):
        """WebSocket网络分区场景"""
        return injector.inject(
            'websocket', FaultType.NETWORK_PARTITION,
            severity=FaultSeverity.HIGH,
            duration=duration,
        )

    @staticmethod
    def memory_pressure(injector: FaultInjector, duration: float = 120):
        """内存压力场景"""
        return injector.inject(
            'system', FaultType.RESOURCE_EXHAUSTION,
            severity=FaultSeverity.HIGH,
            duration=duration,
        )


# 全局故障注入器实例
fault_injector = FaultInjector()
