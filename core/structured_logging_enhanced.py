"""
结构化日志增强
请求上下文自动注入、日志关联、性能标记。

使用方式:
    from core.structured_logging_enhanced import log_context, performance_log
    with log_context(user_id='123', request_id='abc'):
        logger.info("操作完成")

    @performance_log(threshold_ms=100)
    def slow_function():
        ...
"""

import time
import logging
import threading
import functools
from typing import Any, Dict, Optional
from contextlib import contextmanager
from flask import request, g

logger = logging.getLogger(__name__)

# 线程本地存储
_context = threading.local()


def get_context() -> Dict[str, Any]:
    """获取当前日志上下文"""
    if not hasattr(_context, 'data'):
        _context.data = {}
    return _context.data


def set_context(**kwargs):
    """设置日志上下文"""
    ctx = get_context()
    ctx.update(kwargs)


def clear_context():
    """清除日志上下文"""
    if hasattr(_context, 'data'):
        _context.data.clear()


@contextmanager
def log_context(**kwargs):
    """日志上下文管理器"""
    old_context = get_context().copy()
    set_context(**kwargs)
    try:
        yield
    finally:
        _context.data = old_context


class ContextFilter(logging.Filter):
    """日志上下文过滤器"""

    def filter(self, record):
        ctx = get_context()

        # 注入请求上下文
        record.request_id = ctx.get('request_id', '')
        record.user_id = ctx.get('user_id', '')
        record.device_id = ctx.get('device_id', '')
        record.trace_id = ctx.get('trace_id', '')

        # 从Flask请求注入
        try:
            if request:
                if not record.request_id:
                    record.request_id = getattr(g, 'request_id', '')
                if not record.user_id:
                    record.user_id = getattr(g, 'user_id', '')
        except RuntimeError:
            pass  # 非请求上下文

        return True


class PerformanceLogger:
    """性能日志记录器"""

    def __init__(self, threshold_ms: float = 100):
        self.threshold_ms = threshold_ms
        self._records: Dict[str, list] = {}
        self._lock = threading.Lock()

    def log(self, operation: str, duration_ms: float, **extra):
        """记录性能数据"""
        if duration_ms > self.threshold_ms:
            logger.warning(
                "慢操作: %s 耗时 %.1fms (阈值: %.1fms)",
                operation, duration_ms, self.threshold_ms,
                extra={'performance': True, 'duration_ms': duration_ms, **extra}
            )

        with self._lock:
            if operation not in self._records:
                self._records[operation] = []
            self._records[operation].append({
                'duration_ms': duration_ms,
                'timestamp': time.time(),
            })
            # 保留最近100条
            if len(self._records[operation]) > 100:
                self._records[operation] = self._records[operation][-100:]

    def get_stats(self, operation: str) -> Dict[str, Any]:
        """获取操作统计"""
        with self._lock:
            records = self._records.get(operation, [])
            if not records:
                return {'count': 0}

            durations = [r['duration_ms'] for r in records]
            return {
                'count': len(durations),
                'avg_ms': sum(durations) / len(durations),
                'min_ms': min(durations),
                'max_ms': max(durations),
                'p95_ms': sorted(durations)[int(len(durations) * 0.95)] if len(durations) > 1 else durations[0],
            }

    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有操作统计"""
        with self._lock:
            return {op: self.get_stats(op) for op in self._records}


# 全局实例
performance_logger = PerformanceLogger()


def performance_log(threshold_ms: float = 100):
    """性能日志装饰器"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                duration = (time.time() - start) * 1000
                performance_logger.log(func.__name__, duration)
        return wrapper
    return decorator


def init_structured_logging(app):
    """初始化结构化日志"""
    # 添加上下文过滤器
    context_filter = ContextFilter()
    logging.getLogger().addFilter(context_filter)

    # 请求前设置上下文
    @app.before_request
    def before_request_log():
        import uuid
        request_id = request.headers.get('X-Request-ID') or str(uuid.uuid4())[:8]
        set_context(
            request_id=request_id,
            method=request.method,
            path=request.path,
            remote_addr=request.remote_addr,
        )

    # 请求后清除上下文
    @app.teardown_request
    def teardown_request_log(exception=None):
        clear_context()

    logger.info("结构化日志增强已初始化")
