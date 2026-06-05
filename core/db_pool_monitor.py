"""
数据库连接池监控
实时监控SQLite连接池的使用情况，提供指标和告警。

使用方式:
    from core.db_pool_monitor import pool_monitor
    stats = pool_monitor.get_stats()
"""

import time
import sqlite3
import logging
import threading
from typing import Optional, Dict, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class ConnectionPoolMonitor:
    """SQLite连接池监控器"""

    def __init__(self):
        self._active_connections = 0
        self._peak_connections = 0
        self._total_acquired = 0
        self._total_released = 0
        self._total_errors = 0
        self._wait_queue_size = 0
        self._acquire_times: list[float] = []
        self._lock = threading.Lock()
        self._max_history = 100

    def on_acquire(self, start_time: float):
        """连接获取回调"""
        elapsed = time.time() - start_time
        with self._lock:
            self._active_connections += 1
            self._total_acquired += 1
            self._peak_connections = max(self._peak_connections, self._active_connections)
            self._acquire_times.append(elapsed)
            if len(self._acquire_times) > self._max_history:
                self._acquire_times.pop(0)
            if self._wait_queue_size > 0:
                self._wait_queue_size -= 1

    def on_release(self):
        """连接释放回调"""
        with self._lock:
            self._active_connections = max(0, self._active_connections - 1)
            self._total_released += 1

    def on_error(self):
        """连接错误回调"""
        with self._lock:
            self._total_errors += 1

    def on_wait(self):
        """等待连接回调"""
        with self._lock:
            self._wait_queue_size += 1

    def get_stats(self) -> Dict[str, Any]:
        """获取连接池统计"""
        with self._lock:
            avg_acquire = (
                sum(self._acquire_times) / len(self._acquire_times)
                if self._acquire_times else 0
            )
            p95_acquire = (
                sorted(self._acquire_times)[int(len(self._acquire_times) * 0.95)]
                if len(self._acquire_times) >= 2 else avg_acquire
            )
            return {
                'active_connections': self._active_connections,
                'peak_connections': self._peak_connections,
                'total_acquired': self._total_acquired,
                'total_released': self._total_released,
                'total_errors': self._total_errors,
                'wait_queue_size': self._wait_queue_size,
                'avg_acquire_ms': round(avg_acquire * 1000, 2),
                'p95_acquire_ms': round(p95_acquire * 1000, 2),
                'error_rate': round(
                    self._total_errors / max(1, self._total_acquired) * 100, 2
                ),
            }

    def reset(self):
        """重置统计"""
        with self._lock:
            self._active_connections = 0
            self._peak_connections = 0
            self._total_acquired = 0
            self._total_released = 0
            self._total_errors = 0
            self._wait_queue_size = 0
            self._acquire_times.clear()


# 全局实例
pool_monitor = ConnectionPoolMonitor()


@contextmanager
def monitored_connection(db_path: str, timeout: float = 30):
    """
    带监控的数据库连接上下文管理器

    Args:
        db_path: 数据库路径
        timeout: 连接超时
    """
    start = time.time()
    pool_monitor.on_wait()
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        pool_monitor.on_acquire(start)
        yield conn
    except Exception as e:
        pool_monitor.on_error()
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            pool_monitor.on_release()
