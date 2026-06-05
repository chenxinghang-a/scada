"""
智能重试机制增强
支持指数退避、抖动、条件重试、重试预算。

使用方式:
    from core.smart_retry import SmartRetry, retry_on_failure
    retry = SmartRetry(max_retries=3, base_delay=1.0)
    result = retry.execute(some_function, arg1, arg2)
"""

import time
import random
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple, Type
from functools import wraps
from enum import Enum

logger = logging.getLogger(__name__)


class RetryStrategy(Enum):
    """重试策略"""
    FIXED = 'fixed'           # 固定间隔
    EXPONENTIAL = 'exponential'  # 指数退避
    LINEAR = 'linear'         # 线性递增
    FIBONACCI = 'fibonacci'   # 斐波那契


class SmartRetry:
    """智能重试器"""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        strategy: RetryStrategy = RetryStrategy.EXPONENTIAL,
        jitter: bool = True,
        jitter_range: float = 0.5,
        retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
        non_retryable_exceptions: Tuple[Type[Exception], ...] = (),
        on_retry: Optional[Callable] = None,
        on_failure: Optional[Callable] = None,
        budget_window: float = 300.0,
        max_budget_retries: int = 50,
    ):
        """
        Args:
            max_retries: 最大重试次数
            base_delay: 基础延迟（秒）
            max_delay: 最大延迟（秒）
            strategy: 重试策略
            jitter: 是否添加抖动
            jitter_range: 抖动范围（0-1）
            retryable_exceptions: 可重试的异常类型
            non_retryable_exceptions: 不可重试的异常类型
            on_retry: 重试回调
            on_failure: 最终失败回调
            budget_window: 预算窗口（秒）
            max_budget_retries: 窗口内最大重试次数
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.strategy = strategy
        self.jitter = jitter
        self.jitter_range = jitter_range
        self.retryable_exceptions = retryable_exceptions
        self.non_retryable_exceptions = non_retryable_exceptions
        self.on_retry = on_retry
        self.on_failure = on_failure
        self.budget_window = budget_window
        self.max_budget_retries = max_budget_retries

        # 重试预算
        self._retry_history: List[float] = []
        self._lock = threading.Lock()

        # 统计
        self._stats = {
            'total_calls': 0,
            'total_retries': 0,
            'total_successes': 0,
            'total_failures': 0,
        }

    def _calculate_delay(self, attempt: int) -> float:
        """计算延迟时间"""
        if self.strategy == RetryStrategy.FIXED:
            delay = self.base_delay
        elif self.strategy == RetryStrategy.EXPONENTIAL:
            delay = self.base_delay * (2 ** attempt)
        elif self.strategy == RetryStrategy.LINEAR:
            delay = self.base_delay * (attempt + 1)
        elif self.strategy == RetryStrategy.FIBONACCI:
            a, b = self.base_delay, self.base_delay
            for _ in range(attempt):
                a, b = b, a + b
            delay = b
        else:
            delay = self.base_delay

        # 限制最大延迟
        delay = min(delay, self.max_delay)

        # 添加抖动
        if self.jitter:
            jitter_amount = delay * self.jitter_range
            delay += random.uniform(-jitter_amount, jitter_amount)
            delay = max(0.1, delay)  # 最小100ms

        return delay

    def _check_budget(self) -> bool:
        """检查重试预算"""
        now = time.time()
        with self._lock:
            # 清理过期记录
            self._retry_history = [
                t for t in self._retry_history
                if now - t < self.budget_window
            ]
            return len(self._retry_history) < self.max_budget_retries

    def _record_retry(self):
        """记录重试"""
        with self._lock:
            self._retry_history.append(time.time())

    def _is_retryable(self, exception: Exception) -> bool:
        """判断异常是否可重试"""
        # 检查不可重试异常
        if self.non_retryable_exceptions and isinstance(exception, self.non_retryable_exceptions):
            return False

        # 检查可重试异常
        return isinstance(exception, self.retryable_exceptions)

    def execute(self, func: Callable, *args, **kwargs) -> Any:
        """
        执行函数，失败时自动重试

        Args:
            func: 要执行的函数
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            函数返回值

        Raises:
            最后一次执行的异常
        """
        self._stats['total_calls'] += 1
        last_exception = None

        for attempt in range(self.max_retries + 1):
            try:
                result = func(*args, **kwargs)
                if attempt > 0:
                    self._stats['total_successes'] += 1
                    logger.info(f"重试成功: {func.__name__} (第{attempt}次重试)")
                return result

            except Exception as e:
                last_exception = e

                # 检查是否可重试
                if not self._is_retryable(e):
                    logger.warning(f"不可重试异常: {func.__name__} - {type(e).__name__}: {e}")
                    break

                # 检查是否达到最大重试次数
                if attempt >= self.max_retries:
                    logger.warning(f"达到最大重试次数: {func.__name__} ({self.max_retries}次)")
                    break

                # 检查重试预算
                if not self._check_budget():
                    logger.warning(f"重试预算耗尽: {func.__name__}")
                    break

                # 计算延迟并重试
                delay = self._calculate_delay(attempt)
                self._record_retry()
                self._stats['total_retries'] += 1

                logger.info(
                    f"重试中: {func.__name__} (第{attempt + 1}次, "
                    f"延迟{delay:.1f}s, 异常: {type(e).__name__}: {e})"
                )

                # 调用重试回调
                if self.on_retry:
                    try:
                        self.on_retry(attempt + 1, e, delay)
                    except Exception:
                        pass

                time.sleep(delay)

        # 所有重试失败
        self._stats['total_failures'] += 1

        if self.on_failure:
            try:
                self.on_failure(last_exception, self.max_retries)
            except Exception:
                pass

        raise last_exception

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            return {
                **self._stats,
                'retry_budget_used': len(self._retry_history),
                'retry_budget_max': self.max_budget_retries,
                'strategy': self.strategy.value,
                'max_retries': self.max_retries,
            }


def retry_on_failure(
    max_retries: int = 3,
    base_delay: float = 1.0,
    strategy: str = 'exponential',
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    non_retryable_exceptions: Tuple[Type[Exception], ...] = (),
):
    """
    重试装饰器

    Args:
        max_retries: 最大重试次数
        base_delay: 基础延迟
        strategy: 重试策略
        retryable_exceptions: 可重试异常
        non_retryable_exceptions: 不可重试异常
    """
    def decorator(func):
        retry = SmartRetry(
            max_retries=max_retries,
            base_delay=base_delay,
            strategy=RetryStrategy(strategy),
            retryable_exceptions=retryable_exceptions,
            non_retryable_exceptions=non_retryable_exceptions,
        )

        @wraps(func)
        def wrapper(*args, **kwargs):
            return retry.execute(func, *args, **kwargs)

        wrapper.retry_stats = retry.get_stats
        return wrapper
    return decorator


# 全局重试统计
_global_retry_stats = SmartRetry()
