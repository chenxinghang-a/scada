"""
请求队列
对昂贵操作（报表生成、批量导出、数据迁移）进行排队处理，
防止并发执行导致资源耗尽。

使用方式:
    from core.request_queue import RequestQueue, queued_task

    report_queue = RequestQueue('report', max_workers=2)

    @app.route('/api/export/report')
    @queued_task(report_queue)
    def generate_report():
        ...
"""

import time
import uuid
import logging
import threading
from queue import Queue, Empty
from typing import Any, Callable, Optional
from functools import wraps
from flask import jsonify, request

logger = logging.getLogger(__name__)


class TaskStatus:
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'


class RequestQueue:
    """
    请求队列管理器

    Args:
        name: 队列名称
        max_workers: 最大并发工作线程数
        max_queue_size: 最大排队数
    """

    def __init__(self, name: str, max_workers: int = 2, max_queue_size: int = 10):
        self.name = name
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size

        self._queue: Queue = Queue(maxsize=max_queue_size)
        self._tasks: dict[str, dict] = {}
        self._tasks_lock = threading.Lock()
        self._active_workers = 0
        self._worker_lock = threading.Lock()

        # 启动工作线程
        for i in range(max_workers):
            t = threading.Thread(target=self._worker, daemon=True, name=f"rq-{name}-{i}")
            t.start()

    def submit(self, func: Callable, *args, **kwargs) -> str:
        """
        提交任务到队列

        Returns:
            任务ID
        """
        task_id = str(uuid.uuid4())[:8]

        with self._tasks_lock:
            self._tasks[task_id] = {
                'id': task_id,
                'status': TaskStatus.PENDING,
                'created_at': time.time(),
                'started_at': None,
                'completed_at': None,
                'result': None,
                'error': None,
                'queue_position': self._queue.qsize() + 1,
            }

        try:
            self._queue.put((task_id, func, args, kwargs), timeout=1)
        except Exception:
            with self._tasks_lock:
                self._tasks[task_id]['status'] = TaskStatus.FAILED
                self._tasks[task_id]['error'] = '队列已满，请稍后重试'
            return task_id

        logger.info("任务已入队: %s/%s (队列长度=%d)", self.name, task_id, self._queue.qsize())
        return task_id

    def get_status(self, task_id: str) -> Optional[dict]:
        """获取任务状态"""
        with self._tasks_lock:
            return self._tasks.get(task_id)

    def get_stats(self) -> dict:
        """获取队列统计"""
        with self._tasks_lock:
            pending = sum(1 for t in self._tasks.values() if t['status'] == TaskStatus.PENDING)
            running = sum(1 for t in self._tasks.values() if t['status'] == TaskStatus.RUNNING)
            completed = sum(1 for t in self._tasks.values() if t['status'] == TaskStatus.COMPLETED)
            failed = sum(1 for t in self._tasks.values() if t['status'] == TaskStatus.FAILED)
            return {
                'name': self.name,
                'queue_size': self._queue.qsize(),
                'max_workers': self.max_workers,
                'active_workers': self._active_workers,
                'pending': pending,
                'running': running,
                'completed': completed,
                'failed': failed,
                'total_tasks': len(self._tasks),
            }

    def _worker(self):
        """工作线程"""
        while True:
            try:
                task_id, func, args, kwargs = self._queue.get(timeout=300)
            except Empty:
                continue

            with self._worker_lock:
                self._active_workers += 1

            with self._tasks_lock:
                task = self._tasks.get(task_id)
                if task:
                    task['status'] = TaskStatus.RUNNING
                    task['started_at'] = time.time()

            try:
                result = func(*args, **kwargs)
                with self._tasks_lock:
                    task = self._tasks.get(task_id)
                    if task:
                        task['status'] = TaskStatus.COMPLETED
                        task['result'] = result
                        task['completed_at'] = time.time()
                        duration = task['completed_at'] - task['started_at']
                        logger.info("任务完成: %s/%s (%.1fs)", self.name, task_id, duration)
            except Exception as e:
                with self._tasks_lock:
                    task = self._tasks.get(task_id)
                    if task:
                        task['status'] = TaskStatus.FAILED
                        task['error'] = str(e)
                        task['completed_at'] = time.time()
                logger.error("任务失败: %s/%s - %s", self.name, task_id, e)
            finally:
                with self._worker_lock:
                    self._active_workers -= 1
                self._queue.task_done()


def queued_task(queue: RequestQueue):
    """
    队列任务装饰器

    将请求提交到队列，立即返回任务ID，客户端可轮询查询状态。
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            task_id = queue.submit(f, *args, **kwargs)
            status = queue.get_status(task_id)

            if status and status['status'] == TaskStatus.FAILED:
                return jsonify({
                    'success': False,
                    'error': status['error'],
                    'task_id': task_id,
                }), 503

            return jsonify({
                'success': True,
                'message': '任务已提交',
                'task_id': task_id,
                'queue_position': status.get('queue_position', 0) if status else 0,
                'status_url': f'/api/tasks/{task_id}',
            }), 202

        return decorated
    return decorator


# 全局队列实例
report_queue = RequestQueue('report', max_workers=2, max_queue_size=10)
export_queue = RequestQueue('export', max_workers=3, max_queue_size=20)
