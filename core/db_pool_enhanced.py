"""
数据库连接池增强
连接健康检查+自动回收+连接泄漏检测增强。

使用方式:
    from core.db_pool_enhanced import EnhancedConnectionPool
    pool = EnhancedConnectionPool(db_path, max_connections=20)
"""

import time
import sqlite3
import logging
import threading
from typing import Any, Dict, List, Optional
from contextlib import contextmanager
from collections import defaultdict

logger = logging.getLogger(__name__)


class PooledConnection:
    """池化连接"""

    def __init__(self, conn: sqlite3.Connection, conn_id: str):
        self.conn = conn
        self.conn_id = conn_id
        self.created_at = time.time()
        self.last_used = time.time()
        self.use_count = 0
        self.in_use = False
        self._lock = threading.Lock()

    def acquire(self):
        """获取连接"""
        with self._lock:
            self.in_use = True
            self.last_used = time.time()
            self.use_count += 1

    def release(self):
        """释放连接"""
        with self._lock:
            self.in_use = False
            self.last_used = time.time()

    def is_expired(self, max_idle: float = 300) -> bool:
        """检查是否过期"""
        return not self.in_use and (time.time() - self.last_used) > max_idle

    def is_alive(self) -> bool:
        """检查连接是否存活"""
        try:
            self.conn.execute("SELECT 1")
            return True
        except Exception:
            return False


class EnhancedConnectionPool:
    """增强型连接池"""

    def __init__(
        self,
        db_path: str,
        max_connections: int = 20,
        min_connections: int = 2,
        max_idle_time: float = 300,
        health_check_interval: float = 60,
    ):
        self.db_path = db_path
        self.max_connections = max_connections
        self.min_connections = min_connections
        self.max_idle_time = max_idle_time
        self.health_check_interval = health_check_interval

        self._pool: List[PooledConnection] = []
        self._lock = threading.Lock()
        self._conn_counter = 0

        # 统计
        self._stats = {
            'created': 0,
            'acquired': 0,
            'released': 0,
            'expired': 0,
            'health_failures': 0,
            'peak_size': 0,
        }

        # 泄漏检测
        self._active_connections: Dict[str, float] = {}  # conn_id -> acquire_time
        self._leak_threshold = 60  # 秒

        # 启动健康检查线程
        self._health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._health_thread.start()

    def _create_connection(self) -> PooledConnection:
        """创建新连接"""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        self._conn_counter += 1
        conn_id = f"conn_{self._conn_counter:06d}"

        pooled = PooledConnection(conn, conn_id)
        self._stats['created'] += 1

        return pooled

    @contextmanager
    def acquire(self):
        """获取连接"""
        conn = None
        with self._lock:
            # 尝试复用空闲连接
            for pooled in self._pool:
                if not pooled.in_use:
                    pooled.acquire()
                    conn = pooled
                    break

            # 创建新连接
            if conn is None:
                if len(self._pool) < self.max_connections:
                    conn = self._create_connection()
                    conn.acquire()
                    self._pool.append(conn)
                else:
                    # 等待连接释放
                    pass

            if conn:
                self._active_connections[conn.conn_id] = time.time()
                self._stats['acquired'] += 1
                self._stats['peak_size'] = max(self._stats['peak_size'], len(self._pool))

        if conn is None:
            raise RuntimeError("连接池已满，无法获取连接")

        try:
            yield conn.conn
        finally:
            conn.release()
            with self._lock:
                self._active_connections.pop(conn.conn_id, None)
                self._stats['released'] += 1

    def _health_check_loop(self):
        """健康检查循环"""
        while True:
            time.sleep(self.health_check_interval)
            try:
                self._health_check()
                self._cleanup_expired()
                self._detect_leaks()
            except Exception as e:
                logger.error(f"连接池健康检查异常: {e}")

    def _health_check(self):
        """健康检查"""
        with self._lock:
            for pooled in list(self._pool):
                if not pooled.in_use and not pooled.is_alive():
                    try:
                        pooled.conn.close()
                    except Exception:
                        pass
                    self._pool.remove(pooled)
                    self._stats['health_failures'] += 1
                    logger.warning(f"连接池健康检查: 移除失效连接 {pooled.conn_id}")

    def _cleanup_expired(self):
        """清理过期连接"""
        with self._lock:
            min_keep = self.min_connections
            to_remove = []

            for pooled in self._pool:
                if not pooled.in_use and pooled.is_expired(self.max_idle_time):
                    if len(self._pool) - len(to_remove) > min_keep:
                        to_remove.append(pooled)

            for pooled in to_remove:
                try:
                    pooled.conn.close()
                except Exception:
                    pass
                self._pool.remove(pooled)
                self._stats['expired'] += 1

            if to_remove:
                logger.debug(f"连接池清理: 移除 {len(to_remove)} 个过期连接")

    def _detect_leaks(self):
        """检测连接泄漏"""
        now = time.time()
        with self._lock:
            for conn_id, acquire_time in list(self._active_connections.items()):
                if now - acquire_time > self._leak_threshold:
                    logger.warning(
                        f"疑似连接泄漏: {conn_id} 已持有 {now - acquire_time:.0f}秒"
                    )

    def get_stats(self) -> Dict[str, Any]:
        """获取连接池统计"""
        with self._lock:
            active = sum(1 for p in self._pool if p.in_use)
            idle = sum(1 for p in self._pool if not p.in_use)
            return {
                **self._stats,
                'pool_size': len(self._pool),
                'active_connections': active,
                'idle_connections': idle,
                'max_connections': self.max_connections,
            }

    def shutdown(self):
        """关闭连接池"""
        with self._lock:
            for pooled in self._pool:
                try:
                    pooled.conn.close()
                except Exception:
                    pass
            self._pool.clear()


# 全局实例
_pool: Optional[EnhancedConnectionPool] = None


def get_connection_pool(db_path: str = None, **kwargs) -> EnhancedConnectionPool:
    """获取连接池实例"""
    global _pool
    if _pool is None:
        _pool = EnhancedConnectionPool(db_path or 'data/scada.db', **kwargs)
    return _pool
