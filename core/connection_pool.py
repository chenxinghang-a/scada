"""
连接池管理 - 工业级连接复用
避免频繁创建/销毁连接的开销
"""

import threading
import time
import logging
from typing import Optional, Any, Callable
from collections import OrderedDict

logger = logging.getLogger(__name__)


class PooledConnection:
    """池化连接包装"""
    __slots__ = ('client', 'created_at', 'last_used', 'use_count', 'in_use', 'healthy')

    def __init__(self, client: Any):
        self.client = client
        self.created_at = time.time()
        self.last_used = time.time()
        self.use_count = 0
        self.in_use = False
        self.healthy = True


class ConnectionPool:
    """
    通用连接池

    支持连接复用、空闲超时、最大生命周期、健康检查、后台清理。
    线程安全，使用 RLock 保护所有共享状态。

    Args:
        factory: 连接工厂函数，接受 key 参数，返回客户端对象
        max_size: 池最大容量
        min_idle: 最小空闲连接数（预留，暂不实现主动补充）
        max_idle_time: 空闲连接最大存活时间（秒）
        max_lifetime: 连接最大生命周期（秒）
        health_check: 可选的健康检查函数，接受 client 参数，返回 bool
        name: 池名称（用于日志和线程命名）
    """

    def __init__(
        self,
        factory: Callable[[str], Any],
        max_size: int = 20,
        min_idle: int = 2,
        max_idle_time: float = 300.0,
        max_lifetime: float = 3600.0,
        health_check: Optional[Callable[[Any], bool]] = None,
        name: str = "pool",
    ):
        self._factory = factory
        self._max_size = max_size
        self._min_idle = min_idle
        self._max_idle_time = max_idle_time
        self._max_lifetime = max_lifetime
        self._health_check = health_check
        self._name = name

        self._pool: OrderedDict[str, PooledConnection] = OrderedDict()
        self._lock = threading.RLock()
        self._stats = {
            'created': 0,
            'destroyed': 0,
            'hits': 0,
            'misses': 0,
            'active': 0,
            'idle': 0,
        }

        # 后台清理线程
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name=f"pool-{name}"
        )
        self._running = True
        self._cleanup_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, key: str = "default") -> Optional[Any]:
        """
        获取连接

        优先复用已有空闲连接，否则创建新连接。
        池满时返回 None。

        Args:
            key: 连接标识（通常为 device_id）

        Returns:
            客户端对象，获取失败返回 None
        """
        with self._lock:
            # 尝试复用空闲连接
            if key in self._pool:
                conn = self._pool[key]
                if not conn.in_use and conn.healthy:
                    now = time.time()
                    # 检查生命周期和空闲超时
                    if now - conn.created_at >= self._max_lifetime:
                        self._destroy_connection(key, conn)
                    elif now - conn.last_used >= self._max_idle_time:
                        self._destroy_connection(key, conn)
                    else:
                        # 运行健康检查
                        if self._health_check and not self._health_check(conn.client):
                            conn.healthy = False
                            logger.warning(f"连接池 {self._name}: 连接 {key} 健康检查失败")
                            # 销毁不健康连接，避免资源泄漏
                            self._destroy_connection(key, conn)
                        else:
                            conn.in_use = True
                            conn.last_used = time.time()
                            conn.use_count += 1
                            self._stats['hits'] += 1
                            self._stats['active'] += 1
                            self._stats['idle'] -= 1
                            return conn.client

            # 创建新连接
            if len(self._pool) < self._max_size:
                self._stats['misses'] += 1
                return self._create_connection(key)

            logger.warning(f"连接池 {self._name} 已满 ({self._max_size})")
            self._stats['misses'] += 1
            return None

    def release(self, key: str, healthy: bool = True) -> None:
        """
        释放连接回池

        Args:
            key: 连接标识
            healthy: 连接是否健康，False 时会被标记为不健康等待清理
        """
        with self._lock:
            if key in self._pool:
                conn = self._pool[key]
                conn.in_use = False
                conn.healthy = healthy
                conn.last_used = time.time()
                self._stats['active'] -= 1
                self._stats['idle'] += 1

                if not healthy:
                    logger.warning(f"连接池 {self._name}: 连接 {key} 标记为不健康")

    def get_or_create(self, key: str = "default") -> Optional[Any]:
        """
        获取已有连接或创建新连接（不改变使用状态）

        适用于只需要查看/临时使用的场景。
        """
        with self._lock:
            if key in self._pool:
                conn = self._pool[key]
                if conn.healthy:
                    return conn.client
                # 销毁不健康连接再创建新的
                self._destroy_connection(key, conn)
            return self._create_connection(key)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _create_connection(self, key: str) -> Optional[Any]:
        """创建新连接"""
        try:
            client = self._factory(key)
            if client is None:
                logger.error(f"连接池 {self._name}: 工厂函数返回 None (key={key})")
                return None
            conn = PooledConnection(client)
            conn.in_use = True
            conn.use_count = 1
            self._pool[key] = conn
            self._stats['created'] += 1
            self._stats['active'] += 1
            logger.debug(f"连接池 {self._name}: 创建连接 {key}")
            return client
        except Exception as e:
            logger.error(f"连接池 {self._name}: 创建连接失败 {key}: {e}")
            return None

    def _destroy_connection(self, key: str, conn: PooledConnection) -> None:
        """销毁连接"""
        try:
            if hasattr(conn.client, 'disconnect'):
                conn.client.disconnect()
            elif hasattr(conn.client, 'close'):
                conn.client.close()
        except Exception as e:
            logger.debug(f"连接池 {self._name}: 销毁连接 {key} 时清理失败: {e}")
        if key in self._pool:
            del self._pool[key]
        self._stats['destroyed'] += 1
        if conn.in_use:
            self._stats['active'] -= 1
        else:
            self._stats['idle'] -= 1
        logger.debug(f"连接池 {self._name}: 销毁连接 {key}")

    def _cleanup_loop(self) -> None:
        """后台清理过期和不健康连接"""
        while self._running:
            time.sleep(60)
            with self._lock:
                now = time.time()
                expired = []
                for key, conn in self._pool.items():
                    if conn.in_use:
                        continue
                    # 空闲超时
                    if now - conn.last_used > self._max_idle_time:
                        expired.append(key)
                    # 生命周期超时
                    elif now - conn.created_at > self._max_lifetime:
                        expired.append(key)
                    # 不健康
                    elif not conn.healthy:
                        expired.append(key)
                    # 主动健康检查
                    elif self._health_check and not self._health_check(conn.client):
                        conn.healthy = False
                        expired.append(key)

                for key in expired:
                    self._destroy_connection(key, self._pool[key])

    # ------------------------------------------------------------------
    # Stats & Lifecycle
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """获取池统计"""
        with self._lock:
            return {**self._stats, 'total': len(self._pool)}

    def contains(self, key: str) -> bool:
        """检查池中是否存在某 key 的连接"""
        with self._lock:
            return key in self._pool

    def remove(self, key: str) -> bool:
        """从池中移除并销毁指定连接"""
        with self._lock:
            if key in self._pool:
                self._destroy_connection(key, self._pool[key])
                return True
            return False

    def shutdown(self) -> None:
        """关闭连接池，销毁所有连接"""
        self._running = False
        # 等待清理线程退出
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)
        with self._lock:
            for key in list(self._pool.keys()):
                self._destroy_connection(key, self._pool[key])
        logger.info(f"连接池 {self._name} 已关闭")

    def __len__(self) -> int:
        with self._lock:
            return len(self._pool)

    def __contains__(self, key: str) -> bool:
        return self.contains(key)

    def __repr__(self) -> str:
        stats = self.get_stats()
        return (
            f"ConnectionPool(name={self._name!r}, total={stats['total']}, "
            f"active={stats['active']}, idle={stats['idle']}, "
            f"created={stats['created']}, hits={stats['hits']})"
        )
