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

#: `_tasks` 里最多保留多少条**已结束**任务的历史记录。
#
# 背景（2026-09 审计）：`submit()` 每次都往 `self._tasks` 写一条记录，
# 而全文件**没有任何 pop/del/clear** —— 每条记录还持有 `result` 引用。
# 长期运行的服务（报表生成、批量导出、数据迁移都走这个队列）会因此
# 无界增长，最终 OOM。
#
# 取值权衡：太小则运维刚提交完就查不到结果，太大则起不到回收作用。
# 500 条足够覆盖「最近一批任务」的查询需求，且单条记录很小。
MAX_RETAINED_TASKS = 500


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
        # 入队失败计数（队列满导致的丢弃，必须可观测）
        self._enqueue_failures = 0

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
            self._evict_finished_tasks()
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
        except Exception as e:
            # 入队失败：状态被标成 FAILED 且写了错误原因，但**必须同时落日志 + 计数**。
            # 旧实现只有状态字段，调用方若只拿 task_id 不去查状态，
            # "任务没进队列"就完全不可见（前端看到的只是一个永不完成的任务）。
            with self._tasks_lock:
                self._tasks[task_id]['status'] = TaskStatus.FAILED
                self._tasks[task_id]['error'] = '队列已满，请稍后重试'
            self._enqueue_failures += 1
            logger.warning(
                "任务入队失败(%s): task=%s 队列已满(累计 %d 次): %s",
                self.name, task_id, self._enqueue_failures, e)
            return task_id

        logger.info("任务已入队: %s/%s (队列长度=%d)", self.name, task_id, self._queue.qsize())
        return task_id

    def _evict_finished_tasks(self) -> None:
        """回收已结束的旧任务记录，避免 `_tasks` 无界增长。

        **调用方必须已持有 `self._tasks_lock`。**

        只淘汰终态（COMPLETED / FAILED）的记录，且优先淘汰最老的 ——
        排队中/执行中的任务绝不能动，否则 `get_status()` 会突然查不到任务。
        保留最近 ``MAX_RETAINED_TASKS`` 条，保证运维仍能查到近期结果。
        """
        if len(self._tasks) <= MAX_RETAINED_TASKS:
            return

        finished = [
            (tid, t) for tid, t in self._tasks.items()
            if t.get('status') in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        ]
        # 以「完成时间」排序，没有完成时间的退回创建时间
        finished.sort(key=lambda kv: kv[1].get('completed_at') or kv[1].get('created_at') or 0)

        overflow = len(self._tasks) - MAX_RETAINED_TASKS
        for tid, _ in finished[:overflow]:
            self._tasks.pop(tid, None)

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
                # 因队列满被拒的任务数（这些任务从没进过队列，不计入 tasks）
                'enqueue_failures': self._enqueue_failures,
            }

    def _worker(self):
        """工作线程"""
        while True:
            try:
                task_id, func, args, kwargs = self._queue.get(timeout=300)
            except Empty:
                # 安全忽略：队列 300s 超时无任务的正常空转路径，非错误，用于让工作线程持续待命。
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
