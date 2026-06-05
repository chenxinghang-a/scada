"""
优先级请求队列
重要请求优先处理，支持动态优先级调整。

使用方式:
    from core.priority_request_queue import PriorityRequestQueue
    queue = PriorityRequestQueue()
    queue.submit('high', request_func, args)
"""

import time
import heapq
import threading
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import IntEnum
from concurrent.futures import Future, ThreadPoolExecutor

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    """请求优先级"""
    CRITICAL = 0    # 最高优先级（安全相关）
    HIGH = 10       # 高优先级（用户交互）
    NORMAL = 20     # 普通优先级
    LOW = 30        # 低优先级（后台任务）
    BATCH = 40      # 批量操作


@dataclass(order=True)
class PriorityItem:
    """优先级队列项"""
    priority: int
    submit_time: float = field(compare=False)
    func: Callable = field(compare=False)
    args: tuple = field(compare=False)
    kwargs: dict = field(compare=False)
    future: Future = field(compare=False)
    task_id: str = field(compare=False)


class PriorityRequestQueue:
    """优先级请求队列"""

    def __init__(
        self,
        max_workers: int = 10,
        max_queue_size: int = 1000,
        default_timeout: float = 30.0,
    ):
        self._queue: List[PriorityItem] = []
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._max_queue_size = max_queue_size
        self._default_timeout = default_timeout
        self._running = True
        self._task_counter = 0

        # 统计
        self._stats = {
            'submitted': 0,
            'completed': 0,
            'failed': 0,
            'rejected': 0,
        }

        # 启动处理线程
        self._worker_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._worker_thread.start()

    def submit(
        self,
        priority: int,
        func: Callable,
        *args,
        timeout: float = None,
        **kwargs,
    ) -> Tuple[str, Future]:
        """
        提交请求到优先级队列

        Args:
            priority: 优先级（越小越优先）
            func: 要执行的函数
            *args: 位置参数
            timeout: 超时时间
            **kwargs: 关键字参数

        Returns:
            (task_id, future) 元组
        """
        with self._lock:
            if len(self._queue) >= self._max_queue_size:
                self._stats['rejected'] += 1
                raise RuntimeError(f"队列已满 ({self._max_queue_size})")

            self._task_counter += 1
            task_id = f"task_{self._task_counter:06d}"

            future = Future()
            item = PriorityItem(
                priority=priority,
                submit_time=time.time(),
                func=func,
                args=args,
                kwargs=kwargs,
                future=future,
                task_id=task_id,
            )

            heapq.heappush(self._queue, item)
            self._stats['submitted'] += 1

            logger.debug(f"任务入队: {task_id} (优先级={priority}, 队列长度={len(self._queue)})")
            return task_id, future

    def _process_loop(self):
        """处理循环"""
        while self._running:
            item = None
            with self._lock:
                if self._queue:
                    item = heapq.heappop(self._queue)

            if item is None:
                time.sleep(0.01)
                continue

            # 检查超时
            if time.time() - item.submit_time > self._default_timeout:
                item.future.set_timeout_error(TimeoutError("请求超时"))
                self._stats['failed'] += 1
                continue

            # 提交到线程池
            self._executor.submit(self._execute_item, item)

    def _execute_item(self, item: PriorityItem):
        """执行队列项"""
        try:
            result = item.func(*item.args, **item.kwargs)
            item.future.set_result(result)
            self._stats['completed'] += 1
        except Exception as e:
            item.future.set_exception(e)
            self._stats['failed'] += 1
            logger.error(f"任务执行失败: {item.task_id} - {e}")

    def get_stats(self) -> Dict[str, Any]:
        """获取队列统计"""
        with self._lock:
            return {
                **self._stats,
                'queue_size': len(self._queue),
                'max_queue_size': self._max_queue_size,
                'pending_by_priority': self._count_by_priority(),
            }

    def _count_by_priority(self) -> Dict[str, int]:
        """按优先级统计待处理任务"""
        counts = {}
        for item in self._queue:
            name = Priority(item.priority).name if item.priority in Priority._value2member_map_ else str(item.priority)
            counts[name] = counts.get(name, 0) + 1
        return counts

    def shutdown(self, wait: bool = True):
        """关闭队列"""
        self._running = False
        self._executor.shutdown(wait=wait)


# 全局实例
priority_queue = PriorityRequestQueue()


def with_priority(priority: int = Priority.NORMAL):
    """优先级装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            _, future = priority_queue.submit(priority, func, *args, **kwargs)
            return future.result()
        return wrapper
    return decorator
