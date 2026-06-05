"""
慢查询日志记录器
记录执行时间超过阈值的SQL查询，辅助性能分析。

使用方式:
    from core.slow_query_logger import slow_query_wrapper

    # 包装数据库游标
    cursor = slow_query_wrapper(conn.cursor(), threshold_ms=100)
    cursor.execute("SELECT * FROM devices")
"""

import time
import logging
import threading
from typing import Any, Optional
from functools import wraps

logger = logging.getLogger(__name__)


class SlowQueryLogger:
    """慢查询日志记录器"""

    def __init__(self, threshold_ms: float = 100.0, max_history: int = 1000):
        self._threshold_ms = threshold_ms
        self._max_history = max_history
        self._history: list[dict] = []
        self._lock = threading.Lock()
        self._stats = {
            'total_queries': 0,
            'slow_queries': 0,
            'total_time_ms': 0,
        }

    def log_query(self, sql: str, params: Any, duration_ms: float, source: str = ''):
        """记录查询"""
        is_slow = duration_ms >= self._threshold_ms

        with self._lock:
            self._stats['total_queries'] += 1
            self._stats['total_time_ms'] += duration_ms
            if is_slow:
                self._stats['slow_queries'] += 1

                entry = {
                    'sql': sql[:500],  # 截断长SQL
                    'params': str(params)[:200] if params else None,
                    'duration_ms': round(duration_ms, 2),
                    'source': source,
                    'timestamp': time.time(),
                }
                self._history.append(entry)
                if len(self._history) > self._max_history:
                    self._history.pop(0)

                logger.warning(
                    "慢查询: %.1fms | %s | source=%s",
                    duration_ms, sql[:200], source
                )

    def get_stats(self) -> dict:
        """获取统计"""
        with self._lock:
            total = self._stats['total_queries']
            return {
                'total_queries': total,
                'slow_queries': self._stats['slow_queries'],
                'slow_rate': round(
                    self._stats['slow_queries'] / max(1, total) * 100, 2
                ),
                'avg_time_ms': round(
                    self._stats['total_time_ms'] / max(1, total), 2
                ),
                'threshold_ms': self._threshold_ms,
            }

    def get_recent_slow(self, limit: int = 20) -> list[dict]:
        """获取最近的慢查询"""
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def set_threshold(self, ms: float):
        """设置阈值"""
        self._threshold_ms = ms
        logger.info("慢查询阈值已更新: %.1fms", ms)


# 全局实例
slow_query_logger = SlowQueryLogger()


class SlowQueryCursor:
    """慢查询包装游标"""

    def __init__(self, cursor, source: str = ''):
        self._cursor = cursor
        self._source = source

    def execute(self, sql: str, params=None):
        start = time.time()
        try:
            result = self._cursor.execute(sql, params or ())
            return result
        finally:
            duration = (time.time() - start) * 1000
            slow_query_logger.log_query(sql, params, duration, self._source)

    def executemany(self, sql: str, params_list):
        start = time.time()
        try:
            result = self._cursor.executemany(sql, params_list)
            return result
        finally:
            duration = (time.time() - start) * 1000
            log_sql = sql + " [many:" + str(len(params_list)) + "]"
            slow_query_logger.log_query(log_sql, None, duration, self._source)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        return self._cursor.fetchmany(size)

    def close(self):
        return self._cursor.close()

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._cursor.__exit__(*args)


def slow_query_wrapper(cursor, source: str = '', threshold_ms: float = None):
    """包装游标以记录慢查询"""
    if threshold_ms is not None:
        # 创建临时实例使用自定义阈值
        temp_logger = SlowQueryLogger(threshold_ms=threshold_ms)
        wrapper = SlowQueryCursor(cursor, source)
        wrapper._logger = temp_logger
        return wrapper
    return SlowQueryCursor(cursor, source)
