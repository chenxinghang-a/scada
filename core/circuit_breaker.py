"""
熔断器模式 - 快速失败保护
工业SCADA系统中，当下游服务（设备通信、数据库等）持续失败时，
快速失败而非让请求堆积，防止级联故障。

三态模型:
  CLOSED  → 正常放行，统计失败率
  OPEN    → 直接拒绝，等待冷却时间
  HALF_OPEN → 允许少量探测请求，成功则CLOSED，失败则OPEN
"""

import threading
import time
import logging
from enum import Enum
from typing import Callable, Any, Optional, Dict
from functools import wraps

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"         # 正常（放行）
    OPEN = "open"             # 熔断（拒绝）
    HALF_OPEN = "half_open"   # 半开（探测）


class CircuitBreakerError(Exception):
    """熔断器拒绝异常"""
    def __init__(self, breaker_name: str, remaining_seconds: float):
        self.breaker_name = breaker_name
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"熔断器 [{breaker_name}] 已打开，"
            f"剩余冷却 {remaining_seconds:.1f}s"
        )


class CircuitBreaker:
    """
    熔断器

    Args:
        name: 熔断器名称（用于日志和API展示）
        failure_threshold: 连续失败多少次后打开熔断器（默认5）
        recovery_timeout: 熔断器打开后等待多少秒进入半开（默认30）
        half_open_max: 半开状态允许的最大探测请求数（默认3）
        success_threshold: 半开状态连续成功多少次后关闭（默认2）
        excluded_exceptions: 不计入失败的异常类型（如业务逻辑异常）
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 3,
        success_threshold: int = 2,
        excluded_exceptions: tuple = (),
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max
        self.success_threshold = success_threshold
        self.excluded_exceptions = excluded_exceptions

        # 状态
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0       # 半开状态下的连续成功计数
        self._half_open_calls = 0     # 半开状态下的当前探测请求数
        self._last_failure_time = 0.0
        self._last_state_change = time.time()
        self._lock = threading.RLock()

        # 统计
        self._total_calls = 0
        self._total_failures = 0
        self._total_rejections = 0
        self._state_transitions: list = []

    @property
    def state(self) -> CircuitState:
        """获取当前状态（自动检查OPEN→HALF_OPEN转换）"""
        with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.time() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._transition(CircuitState.HALF_OPEN)
            return self._state

    def _transition(self, new_state: CircuitState):
        """状态转换（必须在锁内调用）"""
        old = self._state
        self._state = new_state
        self._last_state_change = time.time()

        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._success_count = 0
            self._half_open_calls = 0
        elif new_state == CircuitState.OPEN:
            self._half_open_calls = 0

        transition = {
            'from': old.value,
            'to': new_state.value,
            'time': self._last_state_change,
        }
        self._state_transitions.append(transition)
        # 保留最近50条转换记录
        if len(self._state_transitions) > 50:
            self._state_transitions = self._state_transitions[-50:]

        logger.warning(
            "熔断器 [%s] 状态转换: %s → %s",
            self.name, old.value, new_state.value
        )

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        通过熔断器执行调用

        Raises:
            CircuitBreakerError: 熔断器打开时
        """
        with self._lock:
            current = self.state  # 触发自动转换检查
            self._total_calls += 1

            if current == CircuitState.OPEN:
                self._total_rejections += 1
                remaining = self.recovery_timeout - (time.time() - self._last_failure_time)
                raise CircuitBreakerError(self.name, max(0, remaining))

            if current == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max:
                    self._total_rejections += 1
                    raise CircuitBreakerError(self.name, 0)
                self._half_open_calls += 1

        # 在锁外执行实际调用（避免长时间持锁）
        try:
            result = func(*args, **kwargs)
        except self.excluded_exceptions:
            raise  # 业务异常不计入失败
        except Exception as e:
            self._on_failure(e)
            raise
        else:
            self._on_success()
            return result

    def _on_success(self):
        """调用成功"""
        with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._transition(CircuitState.CLOSED)

    def _on_failure(self, error: Exception):
        """调用失败"""
        with self._lock:
            self._failure_count += 1
            self._total_failures += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # 半开状态失败 → 直接打开
                self._transition(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._transition(CircuitState.OPEN)

            logger.debug(
                "熔断器 [%s] 失败 %d/%d: %s",
                self.name, self._failure_count, self.failure_threshold, error
            )

    def force_open(self):
        """手动打开熔断器"""
        with self._lock:
            self._transition(CircuitState.OPEN)
            self._last_failure_time = time.time()

    def force_close(self):
        """手动关闭熔断器"""
        with self._lock:
            self._transition(CircuitState.CLOSED)

    def reset(self):
        """重置熔断器"""
        with self._lock:
            self._transition(CircuitState.CLOSED)
            self._total_calls = 0
            self._total_failures = 0
            self._total_rejections = 0

    def get_stats(self) -> Dict[str, Any]:
        """获取熔断器统计信息"""
        with self._lock:
            current = self.state
            failure_rate = (
                self._total_failures / self._total_calls * 100
                if self._total_calls > 0 else 0
            )
            return {
                'name': self.name,
                'state': current.value,
                'failure_count': self._failure_count,
                'failure_threshold': self.failure_threshold,
                'recovery_timeout': self.recovery_timeout,
                'total_calls': self._total_calls,
                'total_failures': self._total_failures,
                'total_rejections': self._total_rejections,
                'failure_rate': round(failure_rate, 2),
                'last_failure': self._last_failure_time,
                'last_state_change': self._last_state_change,
                'recent_transitions': self._state_transitions[-5:],
            }

    def __call__(self, func: Callable) -> Callable:
        """装饰器用法: @breaker"""
        @wraps(func)
        def wrapper(*args, **kwargs):
            return self.call(func, *args, **kwargs)
        wrapper.breaker = self
        return wrapper


class CircuitBreakerManager:
    """
    熔断器管理器 - 全局管理所有熔断器实例

    使用示例:
        manager = CircuitBreakerManager()
        db_breaker = manager.get_or_create('database', failure_threshold=3)
        result = db_breaker.call(db.query, sql)
    """

    def __init__(self):
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 3,
        success_threshold: int = 2,
        excluded_exceptions: tuple = (),
    ) -> CircuitBreaker:
        """获取或创建熔断器"""
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(
                    name=name,
                    failure_threshold=failure_threshold,
                    recovery_timeout=recovery_timeout,
                    half_open_max=half_open_max,
                    success_threshold=success_threshold,
                    excluded_exceptions=excluded_exceptions,
                )
                logger.info("创建熔断器: %s (阈值=%d, 冷却=%ds)",
                            name, failure_threshold, int(recovery_timeout))
            return self._breakers[name]

    def get(self, name: str) -> Optional[CircuitBreaker]:
        """获取熔断器"""
        with self._lock:
            return self._breakers.get(name)

    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有熔断器统计"""
        with self._lock:
            return {name: b.get_stats() for name, b in self._breakers.items()}

    def reset_all(self):
        """重置所有熔断器"""
        with self._lock:
            for breaker in self._breakers.values():
                breaker.reset()
        logger.info("所有熔断器已重置")

    def reset(self, name: str):
        """重置指定熔断器"""
        with self._lock:
            breaker = self._breakers.get(name)
            if breaker:
                breaker.reset()
                logger.info("熔断器 [%s] 已重置", name)


# 全局熔断器管理器
circuit_breaker_manager = CircuitBreakerManager()
