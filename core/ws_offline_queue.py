"""
WebSocket离线消息队列
客户端断线期间缓存消息，重连后批量推送。

使用方式:
    from core.ws_offline_queue import offline_queue
    offline_queue.enqueue('client_sid', {'event': 'data_update', 'data': {...}})
    messages = offline_queue.dequeue('client_sid')
"""

import time
import logging
import threading
from typing import Any, Dict, List, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


class OfflineMessage:
    """离线消息"""

    def __init__(self, event: str, data: Any, priority: int = 0):
        self.event = event
        self.data = data
        self.priority = priority
        self.timestamp = time.time()
        self.attempts = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'event': self.event,
            'data': self.data,
            'timestamp': self.timestamp,
            'attempts': self.attempts,
        }


class OfflineMessageQueue:
    """离线消息队列"""

    def __init__(self, max_messages_per_client: int = 100, max_age_seconds: float = 3600):
        self._queues: Dict[str, List[OfflineMessage]] = defaultdict(list)
        self._lock = threading.Lock()
        self._max_messages = max_messages_per_client
        self._max_age = max_age_seconds
        self._stats = {
            'total_enqueued': 0,
            'total_delivered': 0,
            'total_expired': 0,
        }

    def enqueue(self, client_id: str, event: str, data: Any, priority: int = 0) -> bool:
        """
        入队消息

        Args:
            client_id: 客户端标识
            event: 事件名
            data: 消息数据
            priority: 优先级（越大越优先）

        Returns:
            是否成功入队
        """
        with self._lock:
            queue = self._queues[client_id]

            # 检查队列大小限制
            if len(queue) >= self._max_messages:
                # 移除最旧的低优先级消息
                queue.sort(key=lambda m: (m.priority, m.timestamp))
                queue.pop(0)

            message = OfflineMessage(event, data, priority)
            queue.append(message)
            self._stats['total_enqueued'] += 1

            logger.debug("离线消息入队: client=%s, event=%s, 队列长度=%d",
                         client_id[:8], event, len(queue))
            return True

    def dequeue(self, client_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        出队消息（按优先级和时间排序）

        Args:
            client_id: 客户端标识
            limit: 最大出队数量

        Returns:
            消息列表
        """
        with self._lock:
            queue = self._queues.get(client_id, [])
            if not queue:
                return []

            # 清理过期消息
            now = time.time()
            valid_messages = [m for m in queue if now - m.timestamp < self._max_age]
            expired_count = len(queue) - len(valid_messages)
            self._stats['total_expired'] += expired_count

            # 按优先级和时间排序
            valid_messages.sort(key=lambda m: (-m.priority, m.timestamp))

            # 取出指定数量
            to_deliver = valid_messages[:limit]
            remaining = valid_messages[limit:]

            # 更新队列
            if remaining:
                self._queues[client_id] = remaining
            else:
                del self._queues[client_id]

            # 标记为已尝试
            for msg in to_deliver:
                msg.attempts += 1

            self._stats['total_delivered'] += len(to_deliver)

            logger.info("离线消息出队: client=%s, 数量=%d, 剩余=%d",
                        client_id[:8], len(to_deliver), len(remaining))

            return [msg.to_dict() for msg in to_deliver]

    def peek(self, client_id: str) -> List[Dict[str, Any]]:
        """查看队列中的消息（不移除）"""
        with self._lock:
            queue = self._queues.get(client_id, [])
            return [msg.to_dict() for msg in queue]

    def clear(self, client_id: str) -> int:
        """清空客户端队列"""
        with self._lock:
            queue = self._queues.pop(client_id, [])
            return len(queue)

    def clear_all(self) -> int:
        """清空所有队列"""
        with self._lock:
            total = sum(len(q) for q in self._queues.values())
            self._queues.clear()
            return total

    def get_queue_size(self, client_id: str) -> int:
        """获取客户端队列大小"""
        with self._lock:
            return len(self._queues.get(client_id, []))

    def get_stats(self) -> Dict[str, Any]:
        """获取统计"""
        with self._lock:
            return {
                'active_clients': len(self._queues),
                'total_queued': sum(len(q) for q in self._queues.values()),
                'total_enqueued': self._stats['total_enqueued'],
                'total_delivered': self._stats['total_delivered'],
                'total_expired': self._stats['total_expired'],
                'max_messages_per_client': self._max_messages,
                'max_age_seconds': self._max_age,
            }

    def cleanup_expired(self) -> int:
        """清理所有过期消息"""
        with self._lock:
            now = time.time()
            total_expired = 0

            for client_id in list(self._queues.keys()):
                queue = self._queues[client_id]
                original_len = len(queue)
                self._queues[client_id] = [
                    m for m in queue if now - m.timestamp < self._max_age
                ]
                expired = original_len - len(self._queues[client_id])
                total_expired += expired

                if not self._queues[client_id]:
                    del self._queues[client_id]

            self._stats['total_expired'] += total_expired
            return total_expired


# 全局实例
offline_queue = OfflineMessageQueue()
