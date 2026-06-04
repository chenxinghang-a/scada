"""
事件总线
实现模块间的事件发布/订阅机制
"""

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List
from collections import defaultdict

logger = logging.getLogger(__name__)


class _EventBusCore:
    """事件总线核心实现（实例级状态，多实例互不干扰）"""

    def __init__(self, max_history: int = 1000):
        self._subscribers: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._history: List[Dict[str, Any]] = []
        self._max_history = max_history
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, callback: Callable[..., Any],
                  priority: int = 0, filter_func: Callable[..., bool] = None):
        """订阅事件"""
        with self._lock:
            subscriber = {
                'callback': callback,
                'priority': priority,
                'filter': filter_func
            }
            self._subscribers[event_type].append(subscriber)
            self._subscribers[event_type].sort(key=lambda x: x['priority'], reverse=True)
        logger.debug(f"订阅事件: {event_type} (优先级: {priority})")

    def unsubscribe(self, event_type: str, callback: Callable[..., Any]):
        """取消订阅"""
        with self._lock:
            if event_type in self._subscribers:
                self._subscribers[event_type] = [
                    s for s in self._subscribers[event_type]
                    if s['callback'] != callback
                ]
        logger.debug(f"取消订阅事件: {event_type}")

    def publish(self, event_type: str, data: Any = None, source: str = None):
        """发布事件"""
        event = {
            'type': event_type,
            'data': data,
            'source': source,
            'timestamp': datetime.now()
        }
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history.pop(0)
            subscribers = list(self._subscribers.get(event_type, []))

        for subscriber in subscribers:
            try:
                if subscriber['filter'] and not subscriber['filter'](event):
                    continue
                subscriber['callback'](event)
            except Exception as e:
                logger.error(f"事件处理失败: {event_type}, 错误: {e}")
        logger.debug(f"发布事件: {event_type} (来源: {source})")

    def get_history(self, event_type: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        """获取事件历史"""
        with self._lock:
            if event_type:
                history = [e for e in self._history if e['type'] == event_type]
            else:
                history = self._history.copy()
        return history[-limit:]

    def clear_history(self):
        """清除事件历史"""
        with self._lock:
            self._history.clear()
        logger.debug("清除事件历史")

    def clear_all(self):
        """清除所有订阅和历史"""
        with self._lock:
            self._subscribers.clear()
            self._history.clear()
        logger.debug("清除所有事件订阅和历史")

    def get_subscribers_count(self, event_type: str = None) -> Dict[str, int]:
        """获取订阅者数量"""
        with self._lock:
            if event_type:
                return {event_type: len(self._subscribers.get(event_type, []))}
            return {k: len(v) for k, v in self._subscribers.items()}


# 默认全局实例
_default_bus = _EventBusCore()


class EventBus:
    """
    事件总线（向后兼容包装）

    类方法委托给全局默认实例，保持 EventBus.subscribe(...) 调用方式不变。
    新代码可直接使用 _EventBusCore() 创建独立实例。
    """
    _instance = _default_bus

    @classmethod
    def subscribe(cls, *a, **kw): return cls._instance.subscribe(*a, **kw)
    @classmethod
    def unsubscribe(cls, *a, **kw): return cls._instance.unsubscribe(*a, **kw)
    @classmethod
    def publish(cls, *a, **kw): return cls._instance.publish(*a, **kw)
    @classmethod
    def get_history(cls, *a, **kw): return cls._instance.get_history(*a, **kw)
    @classmethod
    def clear_history(cls, *a, **kw): return cls._instance.clear_history(*a, **kw)
    @classmethod
    def clear_all(cls, *a, **kw): return cls._instance.clear_all(*a, **kw)
    @classmethod
    def get_subscribers_count(cls, *a, **kw): return cls._instance.get_subscribers_count(*a, **kw)
