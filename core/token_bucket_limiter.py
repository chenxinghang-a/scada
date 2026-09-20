"""
令牌桶限流器
基于令牌桶算法的精细限流，支持突发流量和动态调整。

使用方式:
    from core.token_bucket_limiter import TokenBucketLimiter
    limiter = TokenBucketLimiter(capacity=100, refill_rate=10)
    if limiter.allow('user_001'):
        # 处理请求
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它 core 模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 也就是说它宣称的这项能力**当前并未生效**。
#
# 为什么保留而不删除：删掉即丢能力，模块本身有测试价值；这里只把「没接线」显式化、
# 可追踪，避免「代码在库里」被误读成「功能在跑」。
#
# 自动化复核（防止本标注过期）：
#   tests/test_core_regressions.py::test_unwired_marker_matches_reality
#   —— 该用例用 AST 扫描全仓库 import。一旦有人把本模块接进生产代码，
#      而这里仍写着 WIRED = False，用例即失败，强制文档与事实同步。
#
# 接线建议（需改 run.py / 各层，core 内部无权自行接线）：
#     对写接口加 `@token_bucket_required()`，用令牌桶平滑突发流量。
# ============================================================================
WIRED = False


import time
import threading
import logging
from typing import Dict, Optional, Any
from functools import wraps

logger = logging.getLogger(__name__)


class TokenBucket:
    """令牌桶"""

    def __init__(self, capacity: int, refill_rate: float):
        """
        Args:
            capacity: 桶容量（最大令牌数）
            refill_rate: 每秒补充令牌数
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = float(capacity)
        self.last_refill = time.time()
        # 每个桶自带锁：TokenBucketLimiter 的锁只保护"桶字典"，
        # 不保护桶内 tokens 的读-改-写；并发调用 allow() 会各自读到同一个
        # tokens 值再各自扣减 → 实际放行量超过限额（限流静默失效）。
        self._lock = threading.Lock()

    def allow(self, tokens: int = 1) -> bool:
        """检查是否允许请求"""
        with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def get_available(self) -> float:
        """获取可用令牌数（只读，不改状态）"""
        with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            return min(self.capacity, self.tokens + elapsed * self.refill_rate)


class TokenBucketLimiter:
    """令牌桶限流器"""

    def __init__(
        self,
        capacity: int = 100,
        refill_rate: float = 10.0,
        cleanup_interval: int = 300,
    ):
        """
        Args:
            capacity: 默认桶容量
            refill_rate: 默认每秒补充令牌数
            cleanup_interval: 清理间隔（秒）
        """
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()

        # 自定义桶配置
        self._custom_configs: Dict[str, Dict[str, Any]] = {}

    def set_custom_config(self, key: str, capacity: int, refill_rate: float):
        """设置自定义桶配置"""
        self._custom_configs[key] = {
            'capacity': capacity,
            'refill_rate': refill_rate,
        }

    def _get_bucket(self, key: str) -> TokenBucket:
        """获取或创建令牌桶"""
        with self._lock:
            if key not in self._buckets:
                config = self._custom_configs.get(key, {})
                capacity = config.get('capacity', self._capacity)
                refill_rate = config.get('refill_rate', self._refill_rate)
                self._buckets[key] = TokenBucket(capacity, refill_rate)

            # 定期清理
            now = time.time()
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup(now)

            return self._buckets[key]

    def allow(self, key: str, tokens: int = 1) -> bool:
        """检查是否允许请求"""
        bucket = self._get_bucket(key)
        return bucket.allow(tokens)

    def get_available(self, key: str) -> float:
        """获取可用令牌数"""
        bucket = self._get_bucket(key)
        return bucket.get_available()

    def get_status(self, key: str) -> Dict[str, Any]:
        """获取桶状态"""
        bucket = self._get_bucket(key)
        return {
            'key': key,
            'capacity': bucket.capacity,
            'refill_rate': bucket.refill_rate,
            'available_tokens': bucket.get_available(),
            'last_refill': bucket.last_refill,
        }

    def get_all_status(self) -> Dict[str, Any]:
        """获取所有桶状态"""
        with self._lock:
            return {
                'total_buckets': len(self._buckets),
                'default_capacity': self._capacity,
                'default_refill_rate': self._refill_rate,
                'custom_configs': list(self._custom_configs.keys()),
                'buckets': {
                    key: {
                        'capacity': b.capacity,
                        'refill_rate': b.refill_rate,
                        'available': b.get_available(),
                    }
                    for key, b in self._buckets.items()
                },
            }

    def _cleanup(self, now: float):
        """清理过期桶"""
        expired = []
        for key, bucket in self._buckets.items():
            if now - bucket.last_refill > self._cleanup_interval * 2:
                expired.append(key)

        for key in expired:
            del self._buckets[key]

        if expired:
            logger.debug(f"清理过期令牌桶: {len(expired)}个")

        self._last_cleanup = now

    def reset(self, key: str = None):
        """重置桶"""
        with self._lock:
            if key:
                self._buckets.pop(key, None)
            else:
                self._buckets.clear()


# 全局实例
token_bucket_limiter = TokenBucketLimiter()


def token_bucket_required(tokens: int = 1, key_func=None):
    """
    令牌桶限流装饰器

    Args:
        tokens: 消耗令牌数
        key_func: 获取桶key的函数
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if key_func:
                key = key_func()
            else:
                from flask import request
                key = request.remote_addr or 'unknown'

            if not token_bucket_limiter.allow(key, tokens):
                # NOTE: core.service_response 只有 error_response()，没有 api_error()。
                # 旧实现在这里 import api_error → 装饰器一旦被启用就 ImportError，
                # 即"限流装饰器根本没接线"和"接线即崩"两种状态二选一。
                from core.service_response import error_response
                return error_response('请求过于频繁', 429)

            return f(*args, **kwargs)
        return decorated
    return decorator
