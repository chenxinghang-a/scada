"""
数据库连接池增强
连接健康检查+自动回收+连接泄漏检测增强。

使用方式:
    from core.db_pool_enhanced import EnhancedConnectionPool
    pool = EnhancedConnectionPool(db_path, max_connections=20)
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它 core 模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 也就是说它宣称的这项能力**当前并未生效**。
#
# 为什么保留而不删除：删掉即丢能力，模块本身有测试价值；这里只把「没接线」显式化、
# 可追踪，避免「代码在库里」被误读成「功能在跑」。
#
# 自动化复核（防止本标注过期）：
#   tests/test_core_regressions.py::test_unwired_marker_matches_reality
#   —— 该用例用 AST 扫描全仓库 import。一旦有人把本模块接进生产代码，
#      而这里仍写着 WIRED = False，用例即失败，强制文档与事实同步。
#
# 接线建议（需改 run.py / 各层，core 内部无权自行接线）：
#     把 存储层/database.py 的连接获取改为 `with get_connection_pool().acquire() as conn:`。
# ============================================================================
WIRED = False


import time
import sqlite3
import logging
import threading
from typing import Any, Dict, List, Optional
from contextlib import contextmanager
from collections import defaultdict

# paths 是仓库根模块（与 core 同级）。用别名 _paths 避免与局部变量、参数名冲突。
# 部署布局（PyInstaller 产物 / 直接从仓库根启动）下它都在 sys.path 上。
import paths as _paths

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
        except Exception as e:
            # 探活失败返回 False 是安全侧，但原因要留痕：SQLITE_BUSY（只是忙）与
            # SQLITE_CORRUPT（真坏了）都会走到这里，处置方式完全不同。
            logger.debug("连接 %s 存活探测失败: %s", self.conn_id, e)
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
        liveness_probe_interval: float = 30,
    ):
        # 解析为绝对路径：连接池会把该路径直接交给 sqlite3.connect()，
        # 若保持相对路径，从服务/计划任务/冻结产物启动（CWD 非项目根）时
        # 会**静默开出一个空的错误库** —— 查询全部命中不存在的表，
        # 表现为"数据库莫名空了"，而日志里没有任何异常。
        # 与 存储层.database.Database.__init__ / 用户层.audit_logger / timeseries.offline_buffer
        # 保持同一口径（都用 paths.resolve）。
        self.db_path = str(_paths.resolve(db_path))
        self.max_connections = max_connections
        self.min_connections = min_connections
        self.max_idle_time = max_idle_time
        self.health_check_interval = health_check_interval
        self.liveness_probe_interval = liveness_probe_interval

        self._pool: List[PooledConnection] = []
        # RLock + Condition：池满时需要**真的等待**其它线程释放连接，
        # 而 Condition.wait() 依赖锁支持 _release_save/_acquire_restore（RLock 具备）。
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._conn_counter = 0

        # 统计
        self._stats = {
            'created': 0,
            'acquired': 0,
            'released': 0,
            'expired': 0,
            'health_failures': 0,
            'liveness_failures': 0,
            'peak_size': 0,
            'warmup_created': 0,
            # 池满必须可见：旧实现在此静默 `pass`，随后抛 RuntimeError，
            # 调用方看到的是一个没有任何上下文的"池已满"。
            'pool_full_waits': 0,
            'pool_full_timeouts': 0,
        }

        # 泄漏检测
        self._active_connections: Dict[str, float] = {}  # conn_id -> acquire_time
        self._leak_threshold = 60  # 秒

        # 延迟追踪
        self._latency_history: List[float] = []
        self._max_latency_history = 100

        # 启动健康检查线程
        self._health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._health_thread.start()

        # 预热：创建最小连接数
        self._warmup()

    def _warmup(self):
        """预热连接池（创建最小连接数）"""
        with self._lock:
            while len(self._pool) < self.min_connections:
                try:
                    pooled = self._create_connection()
                    self._pool.append(pooled)
                    self._stats['warmup_created'] += 1
                except Exception as e:
                    logger.warning(f"连接池预热失败: {e}")
                    break
        if self._stats['warmup_created'] > 0:
            logger.info(f"连接池预热: 创建 {self._stats['warmup_created']} 个连接")

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
    def acquire(self, timeout: float = 5.0):
        """获取连接

        池满时**真正等待**空闲连接释放（最多 `timeout` 秒），而不是像旧实现那样
        注释写着"等待连接释放"、实际 `pass` 掉直接抛 RuntimeError —— 那种写法
        在注释与行为之间撒了谎，调用方（正确性取决于能否拿到连接）会莫名其妙失败。

        Args:
            timeout: 池满时等待空闲连接的最长时间（秒）

        Raises:
            RuntimeError: 等待超时仍无可用连接。
        """
        conn = None
        deadline = time.monotonic() + timeout
        full_logged = False

        while True:
            with self._cond:
                # 尝试复用空闲连接
                for pooled in self._pool:
                    if not pooled.in_use:
                        pooled.acquire()
                        conn = pooled
                        break

                # 创建新连接
                if conn is None and len(self._pool) < self.max_connections:
                    conn = self._create_connection()
                    conn.acquire()
                    self._pool.append(conn)

                if conn is not None:
                    self._active_connections[conn.conn_id] = time.time()
                    self._stats['acquired'] += 1
                    self._stats['peak_size'] = max(self._stats['peak_size'], len(self._pool))
                    break

                # 池满：等待 release() 唤醒（首次进入打一条 WARNING，避免刷屏）
                self._stats['pool_full_waits'] += 1
                if not full_logged:
                    logger.warning(
                        "连接池已满 (%d/%d)，等待空闲连接释放（最多 %.1fs，累计等待 %d 次）",
                        len(self._pool), self.max_connections, timeout,
                        self._stats['pool_full_waits'])
                    full_logged = True

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stats['pool_full_timeouts'] += 1
                    logger.error(
                        "连接池已满且等待超时 (%.1fs)：%d/%d 个连接全部占用中",
                        timeout, len(self._pool), self.max_connections)
                    break
                self._cond.wait(remaining)

        if conn is None:
            raise RuntimeError(
                f"连接池已满，等待 {timeout:.1f}s 仍无法获取连接"
                f"（{len(self._pool)}/{self.max_connections} 全部占用）")

        try:
            yield conn.conn
        finally:
            conn.release()
            with self._cond:
                self._active_connections.pop(conn.conn_id, None)
                self._stats['released'] += 1
                # 唤醒正在等空闲连接的线程
                self._cond.notify()

    def _health_check_loop(self):
        """健康检查循环"""
        last_liveness = 0
        while True:
            time.sleep(self.health_check_interval)
            try:
                self._health_check()
                self._cleanup_expired()
                self._detect_leaks()

                # 存活探测（按独立间隔）
                now = time.time()
                if now - last_liveness >= self.liveness_probe_interval:
                    self._liveness_probe()
                    last_liveness = now
            except Exception as e:
                logger.error(f"连接池健康检查异常: {e}")

    def _health_check(self):
        """健康检查"""
        with self._lock:
            for pooled in list(self._pool):
                if not pooled.in_use and not pooled.is_alive():
                    try:
                        pooled.conn.close()
                    except Exception as e:
                        # 预期内且无副作用：该连接已被判定失效并将从池中移除，关闭失败不影响后续逻辑
                        logger.debug(f"健康检查中关闭失效连接失败(连接将丢弃): {pooled.conn_id}: {e}")
                    self._pool.remove(pooled)
                    self._stats['health_failures'] += 1
                    logger.warning(f"连接池健康检查: 移除失效连接 {pooled.conn_id}")

    def _liveness_probe(self):
        """存活探测（ping所有连接）"""
        failed = 0
        with self._lock:
            for pooled in list(self._pool):
                if not pooled.in_use:
                    try:
                        start = time.time()
                        pooled.conn.execute("SELECT 1")
                        latency = (time.time() - start) * 1000
                        self._latency_history.append(latency)
                        if len(self._latency_history) > self._max_latency_history:
                            self._latency_history = self._latency_history[-self._max_latency_history:]
                    except Exception:
                        failed += 1
                        self._stats['liveness_failures'] += 1
                        try:
                            pooled.conn.close()
                        except Exception as e:
                            # 预期内且无副作用：探测已失败的连接即将被移除，关闭失败不影响后续逻辑
                            logger.debug(f"存活探测中关闭失败连接失败(连接将丢弃): {pooled.conn_id}: {e}")
                        self._pool.remove(pooled)

        if failed > 0:
            logger.warning(f"连接池存活探测: {failed} 个连接失败")

    def get_latency_stats(self) -> Dict[str, Any]:
        """获取延迟统计"""
        with self._lock:
            if not self._latency_history:
                return {'avg_ms': 0, 'p50_ms': 0, 'p95_ms': 0, 'p99_ms': 0, 'samples': 0}
            sorted_lat = sorted(self._latency_history)
            n = len(sorted_lat)
            return {
                'avg_ms': round(sum(sorted_lat) / n, 2),
                'p50_ms': round(sorted_lat[n // 2], 2),
                'p95_ms': round(sorted_lat[int(n * 0.95)], 2),
                'p99_ms': round(sorted_lat[int(n * 0.99)], 2),
                'min_ms': round(sorted_lat[0], 2),
                'max_ms': round(sorted_lat[-1], 2),
                'samples': n,
            }

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
                except Exception as e:
                    # 预期内且无副作用：该连接已过期并将从池中移除，关闭失败不影响后续逻辑
                    logger.debug(f"关闭过期连接失败(连接将丢弃): {pooled.conn_id}: {e}")
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
            stats = {
                **self._stats,
                'pool_size': len(self._pool),
                'active_connections': active,
                'idle_connections': idle,
                'max_connections': self.max_connections,
            }
        stats['latency'] = self.get_latency_stats()
        return stats

    def shutdown(self):
        """关闭连接池"""
        with self._lock:
            for pooled in self._pool:
                try:
                    pooled.conn.close()
                except Exception as e:
                    # 预期内且无副作用：池即将整体清空，个别连接关闭失败不影响关闭流程
                    logger.debug(f"关闭连接池连接失败(池将清空): {pooled.conn_id}: {e}")
            self._pool.clear()


# 全局实例
_pool: Optional[EnhancedConnectionPool] = None
_pool_lock = threading.Lock()


def get_connection_pool(db_path: str = None, **kwargs) -> EnhancedConnectionPool:
    """获取连接池实例（线程安全单例）"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = EnhancedConnectionPool(db_path or 'data/scada.db', **kwargs)
    return _pool
