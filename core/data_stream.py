"""
数据流式处理增强
实时数据流推送，支持Server-Sent Events和WebSocket流式传输。

使用方式:
    from core.data_stream import DataStreamManager
    manager = DataStreamManager()
    manager.subscribe('device_data', callback)
    manager.publish('device_data', {'device_id': 'pump_001', 'value': 42})
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
#     用 DataStream 替换采集层手写的队列消费循环（需改 采集层/data_collector.py）。
# ============================================================================
WIRED = False


import json
import time
import threading
import logging
from typing import Any, Callable, Dict, List, Optional, Set
from collections import defaultdict
from queue import Queue, Empty

logger = logging.getLogger(__name__)


class StreamSubscription:
    """流订阅"""

    def __init__(self, subscriber_id: str, topic: str, callback: Callable, priority: int = 0):
        self.subscriber_id = subscriber_id
        self.topic = topic
        self.callback = callback
        self.priority = priority
        self.created_at = time.time()
        self.last_delivery = 0
        self.delivery_count = 0
        self.error_count = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'subscriber_id': self.subscriber_id,
            'topic': self.topic,
            'priority': self.priority,
            'created_at': self.created_at,
            'last_delivery': self.last_delivery,
            'delivery_count': self.delivery_count,
            'error_count': self.error_count,
        }


class DataStreamManager:
    """数据流管理器"""

    def __init__(self, max_queue_size: int = 10000):
        self._subscriptions: Dict[str, List[StreamSubscription]] = defaultdict(list)
        self._lock = threading.Lock()
        self._max_queue_size = max_queue_size

        # 消息队列（异步发布）
        self._message_queue: Queue = Queue(maxsize=max_queue_size)
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 统计
        self._stats = {
            'published': 0,
            'delivered': 0,
            'errors': 0,
            'dropped': 0,
        }

        # 启动工作线程
        self._start_worker()

    def _start_worker(self):
        """启动消息分发工作线程"""
        if self._worker_thread and self._worker_thread.is_alive():
            return

        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._process_messages,
            daemon=True,
            name='data-stream-worker'
        )
        self._worker_thread.start()

    def _process_messages(self):
        """处理消息队列"""
        while not self._stop_event.is_set():
            try:
                message = self._message_queue.get(timeout=1.0)
                self._deliver_message(message)
                self._message_queue.task_done()
            except Empty:
                # 安全忽略：队列 1s 超时轮询的正常空转路径，非错误；在此打日志会每秒刷屏。
                continue
            except Exception as e:
                logger.error(f"消息处理异常: {e}")

    def _deliver_message(self, message: Dict[str, Any]):
        """分发消息给订阅者"""
        topic = message.get('topic')
        if not topic:
            return

        with self._lock:
            subscriptions = list(self._subscriptions.get(topic, []))

        # 按优先级排序
        subscriptions.sort(key=lambda s: s.priority, reverse=True)

        for sub in subscriptions:
            try:
                sub.callback(message)
                sub.last_delivery = time.time()
                sub.delivery_count += 1
                self._stats['delivered'] += 1
            except Exception as e:
                sub.error_count += 1
                self._stats['errors'] += 1
                logger.warning(f"消息分发失败: topic={topic}, subscriber={sub.subscriber_id}, error={e}")

    def subscribe(self, topic: str, callback: Callable, subscriber_id: str = None, priority: int = 0) -> str:
        """
        订阅数据流

        Args:
            topic: 主题名称
            callback: 回调函数
            subscriber_id: 订阅者ID（自动生成如果不指定）
            priority: 优先级（越大越先收到）

        Returns:
            订阅者ID
        """
        if not subscriber_id:
            subscriber_id = f"sub_{int(time.time() * 1000)}"

        subscription = StreamSubscription(subscriber_id, topic, callback, priority)

        with self._lock:
            self._subscriptions[topic].append(subscription)

        logger.debug(f"订阅数据流: topic={topic}, subscriber={subscriber_id}")
        return subscriber_id

    def unsubscribe(self, topic: str, subscriber_id: str) -> bool:
        """取消订阅"""
        with self._lock:
            subs = self._subscriptions.get(topic, [])
            for i, sub in enumerate(subs):
                if sub.subscriber_id == subscriber_id:
                    subs.pop(i)
                    logger.debug(f"取消订阅: topic={topic}, subscriber={subscriber_id}")
                    return True
        return False

    def publish(self, topic: str, data: Any, priority: int = 0) -> bool:
        """
        发布消息到数据流

        Args:
            topic: 主题名称
            data: 消息数据
            priority: 消息优先级

        Returns:
            是否成功入队
        """
        message = {
            'topic': topic,
            'data': data,
            'timestamp': time.time(),
            'priority': priority,
        }

        try:
            self._message_queue.put_nowait(message)
            self._stats['published'] += 1
            return True
        except Exception:
            self._stats['dropped'] += 1
            logger.warning(f"消息队列已满，丢弃消息: topic={topic}")
            return False

    def publish_sync(self, topic: str, data: Any) -> int:
        """
        同步发布（立即分发，不经过队列）

        Returns:
            成功分发的订阅者数量
        """
        message = {
            'topic': topic,
            'data': data,
            'timestamp': time.time(),
        }

        with self._lock:
            subscriptions = list(self._subscriptions.get(topic, []))

        delivered = 0
        for sub in subscriptions:
            try:
                sub.callback(message)
                sub.last_delivery = time.time()
                sub.delivery_count += 1
                delivered += 1
            except Exception as e:
                sub.error_count += 1
                logger.warning(f"同步分发失败: {e}")

        return delivered

    def get_subscribers(self, topic: str = None) -> List[Dict[str, Any]]:
        """获取订阅者信息"""
        with self._lock:
            if topic:
                subs = self._subscriptions.get(topic, [])
                return [s.to_dict() for s in subs]
            else:
                result = []
                for topic_subs in self._subscriptions.values():
                    result.extend([s.to_dict() for s in topic_subs])
                return result

    def get_topics(self) -> List[str]:
        """获取所有主题"""
        with self._lock:
            return list(self._subscriptions.keys())

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            total_subscribers = sum(len(subs) for subs in self._subscriptions.values())
            return {
                **self._stats,
                'topics': len(self._subscriptions),
                'total_subscribers': total_subscribers,
                'queue_size': self._message_queue.qsize(),
                'max_queue_size': self._max_queue_size,
            }

    def stop(self):
        """停止流管理器"""
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=5)


# 全局实例
data_stream_manager = DataStreamManager()
