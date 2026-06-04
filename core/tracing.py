"""
分布式追踪模块
为SCADA系统提供请求追踪和性能监控

功能：
- 请求ID生成和传播
- 跨模块追踪
- 性能计时
- 追踪上下文管理
"""

import time
import uuid
import threading
import logging
from contextvars import ContextVar
from typing import Optional, Dict, Any, Callable
from functools import wraps
from datetime import datetime

logger = logging.getLogger(__name__)

# 追踪上下文变量
trace_id_var: ContextVar[str] = ContextVar('trace_id', default='')
span_id_var: ContextVar[str] = ContextVar('span_id', default='')
parent_span_id_var: ContextVar[str] = ContextVar('parent_span_id', default='')


class Span:
    """追踪跨度"""

    def __init__(self, name: str, trace_id: str = None, parent_span_id: str = None):
        self.name = name
        self.trace_id = trace_id or trace_id_var.get() or str(uuid.uuid4())
        self.span_id = str(uuid.uuid4())
        self.parent_span_id = parent_span_id or span_id_var.get()
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.attributes: Dict[str, Any] = {}
        self.events: list = []
        self.status: str = 'ok'
        self.error: Optional[Exception] = None

    def set_attribute(self, key: str, value: Any):
        """设置属性"""
        self.attributes[key] = value

    def add_event(self, name: str, attributes: Dict[str, Any] = None):
        """添加事件"""
        self.events.append({
            'name': name,
            'timestamp': time.time(),
            'attributes': attributes or {},
        })

    def set_error(self, error: Exception):
        """标记错误"""
        self.error = error
        self.status = 'error'
        self.attributes['error.type'] = type(error).__name__
        self.attributes['error.message'] = str(error)

    def finish(self):
        """完成跨度"""
        self.end_time = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'trace_id': self.trace_id,
            'span_id': self.span_id,
            'parent_span_id': self.parent_span_id,
            'name': self.name,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration_ms': round((self.end_time - self.start_time) * 1000, 2) if self.end_time else None,
            'attributes': self.attributes,
            'events': self.events,
            'status': self.status,
        }


class Tracer:
    """追踪器"""

    def __init__(self):
        self._spans: list = []
        self._lock = threading.Lock()
        self._enabled = True

    def start_span(self, name: str, attributes: Dict[str, Any] = None) -> Span:
        """开始一个新的跨度"""
        if not self._enabled:
            return Span(name)

        span = Span(name)

        # 设置上下文
        trace_id_var.set(span.trace_id)
        parent_span_id_var.set(span.span_id)
        span_id_var.set(span.span_id)

        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, v)

        return span

    def end_span(self, span: Span):
        """结束跨度"""
        if not self._enabled:
            return

        span.finish()

        with self._lock:
            self._spans.append(span.to_dict())

            # 只保留最近1000个跨度
            if len(self._spans) > 1000:
                self._spans = self._spans[-1000:]

    def get_trace(self, trace_id: str) -> list:
        """获取追踪链"""
        with self._lock:
            return [s for s in self._spans if s['trace_id'] == trace_id]

    def get_recent_spans(self, limit: int = 100) -> list:
        """获取最近的跨度"""
        with self._lock:
            return self._spans[-limit:]

    def clear(self):
        """清除所有跨度"""
        with self._lock:
            self._spans.clear()


# 全局追踪器实例
_tracer = Tracer()


def get_tracer() -> Tracer:
    """获取全局追踪器"""
    return _tracer


def trace(name: str = None, attributes: Dict[str, Any] = None):
    """追踪装饰器"""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            span_name = name or f"{func.__module__}.{func.__qualname__}"
            span = _tracer.start_span(span_name, attributes)

            try:
                result = func(*args, **kwargs)
                return result
            except Exception as e:
                span.set_error(e)
                raise
            finally:
                _tracer.end_span(span)

        return wrapper
    return decorator


class TraceContext:
    """追踪上下文管理器"""

    def __init__(self, name: str, attributes: Dict[str, Any] = None):
        self.name = name
        self.attributes = attributes
        self.span: Optional[Span] = None

    def __enter__(self) -> Span:
        self.span = _tracer.start_span(self.name, self.attributes)
        return self.span

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.span:
            if exc_val:
                self.span.set_error(exc_val)
            _tracer.end_span(self.span)
        return False


def get_current_trace_id() -> str:
    """获取当前追踪ID"""
    return trace_id_var.get('')


def get_current_span_id() -> str:
    """获取当前跨度ID"""
    return span_id_var.get('')


def set_trace_context(trace_id: str, span_id: str = ''):
    """设置追踪上下文"""
    trace_id_var.set(trace_id)
    if span_id:
        span_id_var.set(span_id)
