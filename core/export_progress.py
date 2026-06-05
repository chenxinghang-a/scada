"""
数据导出进度追踪
大文件导出时提供进度查询，支持取消操作。

使用方式:
    from core.export_progress import ExportProgressTracker
    tracker = ExportProgressTracker()
    task_id = tracker.start('history_data', total_rows=10000)
    tracker.update(task_id, processed=5000)
    status = tracker.get_status(task_id)
"""

import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class ExportStatus(Enum):
    """导出状态"""
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


class ExportTask:
    """导出任务"""

    def __init__(self, task_id: str, export_type: str, total_rows: int = 0):
        self.task_id = task_id
        self.export_type = export_type
        self.total_rows = total_rows
        self.processed_rows = 0
        self.status = ExportStatus.PENDING
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.error: Optional[str] = None
        self.file_path: Optional[str] = None
        self.file_size: int = 0
        self.metadata: Dict[str, Any] = {}

    @property
    def progress_percent(self) -> float:
        """进度百分比"""
        if self.total_rows <= 0:
            return 0.0
        return min(100.0, (self.processed_rows / self.total_rows) * 100)

    @property
    def elapsed_seconds(self) -> float:
        """已用时间"""
        if not self.started_at:
            return 0.0
        end = self.completed_at or time.time()
        return end - self.started_at

    @property
    def eta_seconds(self) -> Optional[float]:
        """预计剩余时间"""
        if not self.started_at or self.processed_rows == 0:
            return None
        elapsed = self.elapsed_seconds
        if elapsed <= 0:
            return None
        rate = self.processed_rows / elapsed
        remaining = self.total_rows - self.processed_rows
        return remaining / rate if rate > 0 else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'task_id': self.task_id,
            'export_type': self.export_type,
            'status': self.status.value,
            'total_rows': self.total_rows,
            'processed_rows': self.processed_rows,
            'progress_percent': round(self.progress_percent, 1),
            'elapsed_seconds': round(self.elapsed_seconds, 1),
            'eta_seconds': round(self.eta_seconds, 1) if self.eta_seconds else None,
            'file_path': self.file_path,
            'file_size': self.file_size,
            'error': self.error,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'metadata': self.metadata,
        }


class ExportProgressTracker:
    """导出进度追踪器"""

    def __init__(self, max_tasks: int = 50, task_timeout: int = 3600):
        self._tasks: Dict[str, ExportTask] = {}
        self._lock = threading.Lock()
        self._max_tasks = max_tasks
        self._task_timeout = task_timeout

    def start(self, export_type: str, total_rows: int = 0, metadata: Dict[str, Any] = None) -> str:
        """开始新的导出任务"""
        task_id = str(uuid.uuid4())[:8]

        with self._lock:
            # 清理过期任务
            self._cleanup_expired()

            # 检查任务数量限制
            if len(self._tasks) >= self._max_tasks:
                # 移除最旧的已完成任务
                self._remove_oldest_completed()

            task = ExportTask(task_id, export_type, total_rows)
            if metadata:
                task.metadata = metadata
            task.status = ExportStatus.RUNNING
            task.started_at = time.time()
            self._tasks[task_id] = task

        logger.info("导出任务开始: %s (类型=%s, 总行数=%d)", task_id, export_type, total_rows)
        return task_id

    def update(self, task_id: str, processed: int = 0, total: int = None):
        """更新任务进度"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != ExportStatus.RUNNING:
                return

            task.processed_rows = processed
            if total is not None:
                task.total_rows = total

    def complete(self, task_id: str, file_path: str = None, file_size: int = 0):
        """完成任务"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return

            task.status = ExportStatus.COMPLETED
            task.completed_at = time.time()
            task.file_path = file_path
            task.file_size = file_size
            task.processed_rows = task.total_rows

        logger.info("导出任务完成: %s (文件=%s, 大小=%d)", task_id, file_path, file_size)

    def fail(self, task_id: str, error: str):
        """任务失败"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return

            task.status = ExportStatus.FAILED
            task.completed_at = time.time()
            task.error = error

        logger.error("导出任务失败: %s (错误=%s)", task_id, error)

    def cancel(self, task_id: str) -> bool:
        """取消任务"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in (ExportStatus.PENDING, ExportStatus.RUNNING):
                return False

            task.status = ExportStatus.CANCELLED
            task.completed_at = time.time()
            return True

    def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态"""
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def get_all_tasks(self, status: ExportStatus = None) -> List[Dict[str, Any]]:
        """获取所有任务"""
        with self._lock:
            tasks = list(self._tasks.values())
            if status:
                tasks = [t for t in tasks if t.status == status]
            return [t.to_dict() for t in sorted(tasks, key=lambda t: t.created_at, reverse=True)]

    def get_stats(self) -> Dict[str, Any]:
        """获取统计"""
        with self._lock:
            tasks = list(self._tasks.values())
            return {
                'total_tasks': len(tasks),
                'running': sum(1 for t in tasks if t.status == ExportStatus.RUNNING),
                'completed': sum(1 for t in tasks if t.status == ExportStatus.COMPLETED),
                'failed': sum(1 for t in tasks if t.status == ExportStatus.FAILED),
                'cancelled': sum(1 for t in tasks if t.status == ExportStatus.CANCELLED),
            }

    def _cleanup_expired(self):
        """清理过期任务"""
        now = time.time()
        expired = [
            task_id for task_id, task in self._tasks.items()
            if now - task.created_at > self._task_timeout
        ]
        for task_id in expired:
            del self._tasks[task_id]

    def _remove_oldest_completed(self):
        """移除最旧的已完成任务"""
        completed = [
            (task_id, task.completed_at)
            for task_id, task in self._tasks.items()
            if task.status in (ExportStatus.COMPLETED, ExportStatus.FAILED, ExportStatus.CANCELLED)
        ]
        if completed:
            oldest_id = min(completed, key=lambda x: x[1] or 0)[0]
            del self._tasks[oldest_id]


# 全局实例
export_tracker = ExportProgressTracker()
