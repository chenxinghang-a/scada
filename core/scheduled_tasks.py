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
        self.executing = False   # 是否正在执行（用于区分「已启动待命」与「真的卡在执行中」）
        self.last_exec_status: Optional[str] = None  # 最近一次执行结果: success/failed
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
            'executing': self.executing,
            'last_exec_status': self.last_exec_status,
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

    #: `stop()` 等待循环退出的上限（秒）。超时不代表失败 —— 只表示任务正在执行，
    #: 此时 `start()` 会拒绝启动，避免两条循环并存。
    STOP_JOIN_TIMEOUT_S = 5.0

    def __init__(self):
        self._tasks: Dict[str, ScheduledTask] = {}
        # 必须是可重入锁：start_all()/stop_all() 在持锁状态下遍历并调用
        # self.start()/self.stop()，而后者内部又会获取同一把锁。
        # 用普通 threading.Lock 会**直接死锁**（启动所有任务时永久挂住）。
        self._lock = threading.RLock()

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
        """直接添加任务

        若同名任务**已在运行/暂停中**，保留原任务不做替换 —— 否则旧任务的
        线程仍在跑却已从 ``_tasks`` 里被摘掉，从此再也停不掉（永久泄漏），
        而且 ``start_maintenance()`` 这类幂等启动会凭空多出一套线程。
        """
        with self._lock:
            existing = self._tasks.get(name)
            if existing is not None and existing.status in ('running', 'paused'):
                logger.warning("定时任务 %s 已在运行，忽略重复注册", name)
                return existing

            task = ScheduledTask(name, func, interval, description)
            self._tasks[name] = task
        logger.info("添加定时任务: %s (间隔=%ds)", name, int(interval))
        return task

    def start(self, name: str) -> bool:
        """启动任务"""
        with self._lock:
            task = self._tasks.get(name)
            if not task:
                return False
            if task.status == 'running':
                return True

            # ⚠️ 上一条循环还没退出时**不能** clear()。
            #
            # `stop()` 只是 set 事件，循环可能正卡在 `_execute_task`（备份/清理
            # 动辄几十秒）。此时若立刻 start：
            #   1. `clear()` 把事件清掉 → **旧循环被救活**（它下一轮检查时已复位）；
            #   2. 又起一条新线程 → 同一个任务被**两条循环同时执行**
            #      （备份写两份、清理跑两遍、指标重复上报）。
            # 所以这里先确认旧线程真的没了；没有就拒绝本次启动并留痕。
            if task._thread is not None and task._thread.is_alive():
                logger.warning(
                    "定时任务 %s 的上一次循环仍在退出中（可能正在执行），本次启动已忽略",
                    name,
                )
                return False

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
            thread = task._thread

        # join 必须放在锁**外**：循环退出前会回调本管理器（`_execute_task` 里取
        # `self._lock` 改状态），持锁 join 会把对方挡在锁上，只能等到超时。
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.STOP_JOIN_TIMEOUT_S)
            if thread.is_alive():
                logger.warning(
                    "定时任务 %s 在 %.1fs 内未退出（多半正在执行中）；"
                    "在它结束前 start() 会被拒绝，避免同一任务被两条循环同时跑",
                    name, self.STOP_JOIN_TIMEOUT_S,
                )
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
        # 先取名字再逐个启动：`start()` 内部会取同一把（可重入）锁，
        # 且在「旧循环未退出」时会拒绝启动并打日志 —— 在锁内循环调用会让
        # 日志/状态更新与 join 相互干扰，这里统一改成锁外执行。
        with self._lock:
            names = [
                t.name for t in self._tasks.values()
                if t.status in ('pending', 'stopped')
            ]
        for name in names:
            self.start(name)

    def stop_all(self):
        """停止所有任务"""
        with self._lock:
            names = [
                t.name for t in self._tasks.values()
                if t.status in ('running', 'paused')
            ]
        # 锁外逐个 stop：stop() 里有 join（最多 STOP_JOIN_TIMEOUT_S），
        # 持锁 join 会把正在执行的任务线程挡在 self._lock 上，只能等超时。
        for name in names:
            self.stop(name)

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
        """执行单次任务

        ``status`` 同时承担「调度状态」（pending/running/paused/stopped）和
        「正在执行」两种语义，因此执行期间置为 running 后**必须还原**：
        否则一次 run_now() 就会把 pending/stopped 的任务永久钉在 running 上
        （get_status() 永久谎报运行中，start_all() 也会因为状态是 running
        而永远不再启动它）。
        """
        with self._lock:
            prev_status = task.status
            task.executing = True
        start = time.time()
        task.status = 'running'
        task.last_run = start

        try:
            task.func()
            task.run_count += 1
            task.last_error = None
            task.last_exec_status = 'success'
            logger.debug("任务执行完成: %s (%.1fs)", task.name, time.time() - start)
        except Exception as e:
            task.error_count += 1
            task.last_error = str(e)
            task.last_exec_status = 'failed'
            logger.error("任务执行失败: %s - %s", task.name, e)
        finally:
            task.last_duration = time.time() - start
            with self._lock:
                task.executing = False
                # 执行期间被 stop()/pause() 时以新状态为准；否则回到执行前的调度状态
                if task._stop_event.is_set():
                    task.status = 'stopped'
                elif not task._pause_event.is_set():
                    task.status = 'paused'
                else:
                    task.status = prev_status


# 全局实例
task_manager = TaskManager()
