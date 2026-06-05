"""
滑动窗口限流器
更精确的IP限流，避免固定窗口的边界突发问题。

使用方式:
    from core.sliding_window_limiter import sliding_window_limiter
    @app.before_request
    def check_rate_limit():
        if not sliding_window_limiter.allow(request.remote_addr):
            return api_error('请求过于频繁', 429)
"""

import time
import threading
import logging
from typing import Dict, List, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


class SlidingWindowCounter:
    """滑动窗口计数器"""

    def __init__(self, window_size: int = 60, max_requests: int = 100):
        self.window_size = window_size  # 窗口大小（秒）
        self.max_requests = max_requests  # 最大请求数
        self.requests: List[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """检查是否允许请求"""
        now = time.time()
        with self._lock:
            # 清理过期请求
            cutoff = now - self.window_size
            self.requests = [t for t in self.requests if t > cutoff]

            if len(self.requests) < self.max_requests:
                self.requests.append(now)
                return True
            return False

    def get_remaining(self) -> int:
        """获取剩余请求数"""
        now = time.time()
        with self._lock:
            cutoff = now - self.window_size
            self.requests = [t for t in self.requests if t > cutoff]
            return max(0, self.max_requests - len(self.requests))

    def get_retry_after(self) -> float:
        """获取重试等待时间"""
        now = time.time()
        with self._lock:
            if not self.requests:
                return 0
            cutoff = now - self.window_size
            self.requests = [t for t in self.requests if t > cutoff]
            if len(self.requests) < self.max_requests:
                return 0
            return self.requests[0] + self.window_size - now


class SlidingWindowLimiter:
    """滑动窗口限流器"""

    def __init__(
        self,
        default_window: int = 60,
        default_max: int = 100,
        cleanup_interval: int = 300,
    ):
        self.default_window = default_window
        self.default_max = default_max
        self.cleanup_interval = cleanup_interval
        self._counters: Dict[str, SlidingWindowCounter] = {}
        self._endpoint_limits: Dict[str, Dict[str, int]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.time()

    def set_endpoint_limit(self, endpoint: str, window: int, max_requests: int):
        """设置端点限流"""
        with self._lock:
            self._endpoint_limits[endpoint] = {
                'window': window,
                'max': max_requests,
            }

    def _get_counter(self, key: str, endpoint: str = '') -> SlidingWindowCounter:
        """获取或创建计数器"""
        full_key = f"{key}:{endpoint}" if endpoint else key

        with self._lock:
            if full_key not in self._counters:
                limits = self._endpoint_limits.get(endpoint, {})
                window = limits.get('window', self.default_window)
                max_req = limits.get('max', self.default_max)
                self._counters[full_key] = SlidingWindowCounter(window, max_req)
            return self._counters[full_key]

    def allow(self, key: str, endpoint: str = '') -> bool:
        """检查是否允许请求"""
        counter = self._get_counter(key, endpoint)
        allowed = counter.allow()

        if not allowed:
            logger.warning("限流触发: key=%s, endpoint=%s", key, endpoint)

        # 定期清理
        self._maybe_cleanup()

        return allowed

    def get_headers(self, key: str, endpoint: str = '') -> Dict[str, str]:
        """获取限流响应头"""
        counter = self._get_counter(key, endpoint)
        remaining = counter.get_remaining()
        retry_after = counter.get_retry_after()

        limits = self._endpoint_limits.get(endpoint, {})
        max_req = limits.get('max', self.default_max)

        headers = {
            'X-RateLimit-Limit': str(max_req),
            'X-RateLimit-Remaining': str(remaining),
            'X-RateLimit-Reset': str(int(time.time() + retry_after)),
        }

        if remaining == 0:
            headers['Retry-After'] = str(int(retry_after) + 1)

        return headers

    def _maybe_cleanup(self):
        """定期清理过期计数器"""
        now = time.time()
        if now - self._last_cleanup < self.cleanup_interval:
            return

        with self._lock:
            self._last_cleanup = now
            expired_keys = []

            for key, counter in self._counters.items():
                if counter.get_remaining() == self.default_max:
                    expired_keys.append(key)

            for key in expired_keys:
                del self._counters[key]

            if expired_keys:
                logger.debug("清理过期限流计数器: %d个", len(expired_keys))

    def get_stats(self) -> Dict[str, Any]:
        """获取限流统计"""
        with self._lock:
            return {
                'active_counters': len(self._counters),
                'endpoint_limits': dict(self._endpoint_limits),
                'default_window': self.default_window,
                'default_max': self.default_max,
            }


# 全局实例
sliding_window_limiter = SlidingWindowLimiter()
