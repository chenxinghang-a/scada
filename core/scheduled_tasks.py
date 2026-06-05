"""
定时任务管理器
支持任务调度、暂停、恢复、状态查询。

使用方式:
    from core.scheduled_tasks import task_manager

    @task_manager.register('cleanup', interval=3600)
    def cleanup_task():
        ...
"""

import time
import logging
import threading
from typing import Callable, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class ScheduledTask:
    """定时任务"""

    def __init__(self, name: str, func: Callable, interval: float, description: str = ''):
        self.name = name
        self.func = func
        self.interval = interval
        self.description = description
        self.status = 'pending'  # pending/running/paused/stopped
        self.last_run: Optional[float] = None
        self.next_run: Optional[float] = None
        self.run_count = 0
        self.error_count = 0
        self.last_error: Optional[str] = None
        self.last_duration: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始不暂停

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'description': self.description,
            'status': self.status,
            'interval': self.interval,
            'last_run': self.last_run,
            'next_run': self.next_run,
            'run_count': self.run_count,
            'error_count': self.error_count,
            'last_error': self.last_error,
            'last_duration': self.last_duration,
        }


class TaskManager:
    """定时任务管理器"""

    def __init__(self):
        self._tasks: Dict[str, ScheduledTask] = {}
        self._lock = threading.Lock()

    def register(self, name: str, interval: float, description: str = ''):
        """注册定时任务装饰器"""
        def decorator(func: Callable):
            task = ScheduledTask(name, func, interval, description)
            with self._lock:
                self._tasks[name] = task
            logger.info("注册定时任务: %s (间隔=%ds)", name, int(interval))
            return func
        return decorator

    def add(self, name: str, func: Callable, interval: float, description: str = ''):
        """直接添加任务"""
        task = ScheduledTask(name, func, interval, description)
        with self._lock:
            self._tasks[name] = task
        logger.info("添加定时任务: %s (间隔=%ds)", name, int(interval))

    def start(self, name: str) -> bool:
        """启动任务"""
        with self._lock:
            task = self._tasks.get(name)
            if not task:
                return False
            if task.status == 'running':
                return True

            task._stop_event.clear()
            task._pause_event.set()
            task.status = 'running'
            task._thread = threading.Thread(
                target=self._run_loop, args=(task,), daemon=True, name=f"task-{name}"
            )
            task._thread.start()
            logger.info("启动定时任务: %s", name)
            return True

    def stop(self, name: str) -> bool:
        """停止任务"""
        with self._lock:
            task = self._tasks.get(name)
            if not task:
                return False

            task._stop_event.set()
            task._pause_event.set()  # 唤醒暂停的线程
            task.status = 'stopped'
            logger.info("停止定时任务: %s", name)
            return True

    def pause(self, name: str) -> bool:
        """暂停任务"""
        with self._lock:
            task = self._tasks.get(name)
            if not task or task.status != 'running':
                return False

            task._pause_event.clear()
            task.status = 'paused'
            logger.info("暂停定时任务: %s", name)
            return True

    def resume(self, name: str) -> bool:
        """恢复任务"""
        with self._lock:
            task = self._tasks.get(name)
            if not task or task.status != 'paused':
                return False

            task._pause_event.set()
            task.status = 'running'
            logger.info("恢复定时任务: %s", name)
            return True

    def start_all(self):
        """启动所有任务"""
        with self._lock:
            for task in self._tasks.values():
                if task.status in ('pending', 'stopped'):
                    self.start(task.name)

    def stop_all(self):
        """停止所有任务"""
        with self._lock:
            for task in self._tasks.values():
                if task.status in ('running', 'paused'):
                    self.stop(task.name)

    def get_status(self, name: str = None) -> Any:
        """获取任务状态"""
        with self._lock:
            if name:
                task = self._tasks.get(name)
                return task.to_dict() if task else None
            return {name: t.to_dict() for name, t in self._tasks.items()}

    def run_now(self, name: str) -> bool:
        """立即执行一次任务"""
        with self._lock:
            task = self._tasks.get(name)
            if not task:
                return False

        threading.Thread(
            target=self._execute_task, args=(task,), daemon=True
        ).start()
        return True

    def _run_loop(self, task: ScheduledTask):
        """任务运行循环"""
        while not task._stop_event.is_set():
            task._pause_event.wait()  # 等待非暂停状态
            if task._stop_event.is_set():
                break

            task.next_run = time.time() + task.interval
            task._stop_event.wait(task.interval)

            if task._stop_event.is_set():
                break

            task._pause_event.wait()
            if task._stop_event.is_set():
                break

            self._execute_task(task)

    def _execute_task(self, task: ScheduledTask):
        """执行单次任务"""
        start = time.time()
        task.status = 'running'
        task.last_run = start

        try:
            task.func()
            task.run_count += 1
            task.last_error = None
            logger.debug("任务执行完成: %s (%.1fs)", task.name, time.time() - start)
        except Exception as e:
            task.error_count += 1
            task.last_error = str(e)
            logger.error("任务执行失败: %s - %s", task.name, e)
        finally:
            task.last_duration = time.time() - start


# 全局实例
task_manager = TaskManager()
