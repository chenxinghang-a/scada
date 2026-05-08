"""
事件总线
实现模块间的事件发布/订阅机制
"""

import logging
import threading
from typing import Any, Callable, Dict, List
from collections import defaultdict

logger = logging.getLogger(__name__)


class EventBus:
    """
    事件总线
    
    支持：
    1. 同步/异步事件发布
    2. 事件过滤
    3. 优先级
    4. 事件历史
    """
    
    _subscribers: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    _history: List[Dict[str, Any]] = []
    _max_history = 1000
    _lock = threading.Lock()
    
    @classmethod
    def subscribe(cls, event_type: str, callback: Callable[..., Any], 
                  priority: int = 0, filter_func: Callable[..., bool] = None):
        """
        订阅事件
        
        Args:
            event_type: 事件类型
            callback: 回调函数
            priority: 优先级（数字越大优先级越高）
            filter_func: 过滤函数，返回True才调用回调
        """
        with cls._lock:
            subscriber = {
                'callback': callback,
                'priority': priority,
                'filter': filter_func
            }
            cls._subscribers[event_type].append(subscriber)
            
            # 按优先级排序
            cls._subscribers[event_type].sort(key=lambda x: x['priority'], reverse=True)
        
        logger.debug(f"订阅事件: {event_type} (优先级: {priority})")
    
    @classmethod
    def unsubscribe(cls, event_type: str, callback: Callable[..., Any]):
        """
        取消订阅
        
        Args:
            event_type: 事件类型
            callback: 回调函数
        """
        with cls._lock:
            if event_type in cls._subscribers:
                cls._subscribers[event_type] = [
                    s for s in cls._subscribers[event_type]
                    if s['callback'] != callback
                ]
        
        logger.debug(f"取消订阅事件: {event_type}")
    
    @classmethod
    def publish(cls, event_type: str, data: Any = None, source: str = None):
        """
        发布事件
        
        Args:
            event_type: 事件类型
            data: 事件数据
            source: 事件来源
        """
        event = {
            'type': event_type,
            'data': data,
            'source': source,
            'timestamp': threading.current_thread().ident
        }
        
        # 记录历史
        with cls._lock:
            cls._history.append(event)
            if len(cls._history) > cls._max_history:
                cls._history.pop(0)
        
        # 调用订阅者
        subscribers = cls._subscribers.get(event_type, [])
        for subscriber in subscribers:
            try:
                # 检查过滤器
                if subscriber['filter'] and not subscriber['filter'](event):
                    continue
                
                # 调用回调
                subscriber['callback'](event)
            except Exception as e:
                logger.error(f"事件处理失败: {event_type}, 错误: {e}")
        
        logger.debug(f"发布事件: {event_type} (来源: {source})")
    
    @classmethod
    def get_history(cls, event_type: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取事件历史
        
        Args:
            event_type: 事件类型（None则返回所有）
            limit: 返回数量限制
            
        Returns:
            事件历史列表
        """
        with cls._lock:
            if event_type:
                history = [e for e in cls._history if e['type'] == event_type]
            else:
                history = cls._history.copy()
        
        return history[-limit:]
    
    @classmethod
    def clear_history(cls):
        """清除事件历史"""
        with cls._lock:
            cls._history.clear()
        logger.debug("清除事件历史")
    
    @classmethod
    def clear_all(cls):
        """清除所有订阅和历史"""
        with cls._lock:
            cls._subscribers.clear()
            cls._history.clear()
        logger.debug("清除所有事件订阅和历史")
    
    @classmethod
    def get_subscribers_count(cls, event_type: str = None) -> Dict[str, int]:
        """
        获取订阅者数量
        
        Args:
            event_type: 事件类型（None则返回所有）
            
        Returns:
            事件类型到订阅者数量的映射
        """
        if event_type:
            return {event_type: len(cls._subscribers.get(event_type, []))}
        
        return {k: len(v) for k, v in cls._subscribers.items()}
