"""
多级缓存策略
L1内存缓存+L2 SQLite缓存+ETag条件请求组合。

使用方式:
    from core.cache_tier import TieredCache
    cache = TieredCache()
    result = cache.get_or_set('key', expensive_query, ttl=60)
"""

import time
import json
import sqlite3
import hashlib
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


class MemoryCache:
    """L1内存缓存（最快，容量有限）"""

    def __init__(self, max_size: int = 1000, default_ttl: float = 30.0):
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires = entry
            if time.time() > expires:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: float = None):
        with self._lock:
            if len(self._cache) >= self._max_size:
                oldest = min(self._cache.items(), key=lambda x: x[1][1])
                del self._cache[oldest[0]]
            self._cache[key] = (value, time.time() + (ttl or self._default_ttl))

    def delete(self, key: str):
        with self._lock:
            self._cache.pop(key, None)

    def clear(self):
        with self._lock:
            self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            'size': len(self._cache),
            'max_size': self._max_size,
            'hits': self._hits,
            'misses': self._misses,
            'hit_rate': round(self._hits / max(1, total) * 100, 1),
        }


class SQLiteCache:
    """L2 SQLite缓存（持久化，容量大）"""

    def __init__(self, db_path: str = None, default_ttl: float = 300.0):
        if db_path is None:
            db_path = str(Path(__file__).parent.parent / 'data' / 'cache.db')
        self._db_path = db_path
        self._default_ttl = default_ttl
        self._lock = threading.RLock()
        # 失败计数：读/清理失败此前被完全吞掉（get 返回 None、cleanup 返回 0），
        # 运维侧看到的只是"缓存没命中""没东西可清理"，无法区分"真没有"与"读不动"。
        self._errors = {'read': 0, 'write': 0, 'delete': 0, 'cleanup': 0, 'stats': 0}
        self._last_error: Optional[str] = None
        self._init_db()

    def _record_error(self, kind: str, e: Exception, context: str) -> None:
        """记录一次 L2 缓存故障：计数 + WARNING 留痕 + 保存最近原因"""
        self._errors[kind] = self._errors.get(kind, 0) + 1
        self._last_error = f"{context}: {e}"
        logger.warning("L2缓存%s失败(%s)(累计 %d 次): %s",
                       context, kind, self._errors[kind], e)

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)')
        conn.commit()
        conn.close()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            try:
                conn = sqlite3.connect(self._db_path, timeout=5)
                row = conn.execute(
                    'SELECT value, expires_at FROM cache WHERE key = ?', (key,)
                ).fetchone()
                conn.close()

                if row is None:
                    return None
                if time.time() > row[1]:
                    self.delete(key)
                    return None
                return json.loads(row[0])
            except Exception as e:
                # 读失败与"未命中"必须可区分：未命中是正常语义，读失败是故障。
                # 仍按 miss 处理（安全侧），但必须留痕 + 计数，否则缓存层长期失效
                # 而监控上看起来只是"命中率低"。
                self._record_error('read', e, '读取')
                return None

    def set(self, key: str, value: Any, ttl: float = None):
        with self._lock:
            try:
                now = time.time()
                conn = sqlite3.connect(self._db_path, timeout=5)
                conn.execute(
                    'INSERT OR REPLACE INTO cache (key, value, expires_at, created_at) VALUES (?, ?, ?, ?)',
                    (key, json.dumps(value, default=str), now + (ttl or self._default_ttl), now)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                self._record_error('write', e, '写入')

    def delete(self, key: str):
        with self._lock:
            try:
                conn = sqlite3.connect(self._db_path, timeout=5)
                conn.execute('DELETE FROM cache WHERE key = ?', (key,))
                conn.commit()
                conn.close()
            except Exception as e:
                # 删除失败意味着该 key 仍留在 L2，后续 get() 可能命中陈旧值
                self._record_error('delete', e, f'删除(key={key})')

    def cleanup_expired(self) -> int:
        """清理过期缓存

        Returns:
            实际删除条数。清理**失败**时返回 0 并记录 WARNING + 错误计数，
            调用方可用 `get_stats()['errors']` 区分"没有过期项"与"清理失败"。
        """
        with self._lock:
            try:
                conn = sqlite3.connect(self._db_path, timeout=5)
                cursor = conn.execute('DELETE FROM cache WHERE expires_at < ?', (time.time(),))
                deleted = cursor.rowcount
                conn.commit()
                conn.close()
                return deleted
            except Exception as e:
                # 旧实现直接 return 0 —— 与"没有过期项"完全无法区分。
                self._record_error('cleanup', e, '清理过期项')
                return 0

    def get_stats(self) -> Dict[str, Any]:
        """获取 L2 缓存统计

        失败分支与成功分支**结构一致**（都带 count/size_bytes/available/errors），
        调用方不需要为失败路径写特例；失败时 count/size_bytes 为 None 而非 0，
        避免把"读不动"读成"缓存是空的"。
        """
        stats: Dict[str, Any] = {
            'errors': dict(self._errors),
            'last_error': self._last_error,
        }
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            row = conn.execute('SELECT COUNT(*), SUM(LENGTH(value)) FROM cache').fetchone()
            conn.close()
            stats.update({
                'available': True,
                'count': row[0] or 0,
                'size_bytes': row[1] or 0,
            })
        except Exception as e:
            self._record_error('stats', e, '统计')
            stats.update({
                'available': False,
                'count': None,
                'size_bytes': None,
                'error': str(e),
            })
        return stats


class TieredCache:
    """多级缓存管理器"""

    def __init__(self, l1_max: int = 1000, l1_ttl: float = 30.0, l2_ttl: float = 300.0):
        self.l1 = MemoryCache(max_size=l1_max, default_ttl=l1_ttl)
        self.l2 = SQLiteCache(default_ttl=l2_ttl)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """从缓存获取（L1→L2）"""
        # 尝试L1
        result = self.l1.get(key)
        if result is not None:
            return result

        # 尝试L2
        result = self.l2.get(key)
        if result is not None:
            # 回填L1
            self.l1.set(key, result)
            return result

        return None

    def set(self, key: str, value: Any, ttl: float = None, persist: bool = True):
        """写入缓存"""
        self.l1.set(key, value, ttl)
        if persist:
            self.l2.set(key, value, ttl)

    def delete(self, key: str):
        """删除缓存"""
        self.l1.delete(key)
        self.l2.delete(key)

    def get_or_set(self, key: str, factory: Callable, ttl: float = None, persist: bool = True) -> Any:
        """获取缓存或执行工厂函数"""
        result = self.get(key)
        if result is not None:
            return result

        value = factory()
        self.set(key, value, ttl, persist)
        return value

    def invalidate_pattern(self, pattern: str):
        """按前缀失效缓存（L1+L2）"""
        with self.l1._lock:
            keys_to_delete = [k for k in self.l1._cache if k.startswith(pattern)]
            for k in keys_to_delete:
                del self.l1._cache[k]

        # L2也按前缀失效
        try:
            conn = sqlite3.connect(self.l2._db_path, timeout=5)
            conn.execute('DELETE FROM cache WHERE key LIKE ?', (pattern + '%',))
            conn.commit()
            conn.close()
        except Exception as e:
            # 按前缀失效失败 → 相关 key 未被清理，后续可能读到陈旧缓存
            logger.warning(f"L2缓存按前缀失效失败(陈旧数据可能残留): pattern={pattern}: {e}")

    def invalidate_table(self, table: str):
        """按表名失效所有相关缓存"""
        self.invalidate_pattern(f'db:{table}:')

    def warmup(self, loaders: List[Tuple[str, Callable, float]]):
        """
        缓存预热

        Args:
            loaders: [(key, factory_fn, ttl), ...] 列表
        """
        warmed = 0
        failed: List[str] = []
        for key, factory, ttl in loaders:
            try:
                cached = self.get(key)
                if cached is None:
                    value = factory()
                    self.set(key, value, ttl)
                    warmed += 1
            except Exception as e:
                failed.append(key)
                logger.warning(f"缓存预热失败: {key}: {e}")
        # 汇总行必须如实反映失败数，否则"预热完成 N/M"会被读成"其余只是无需预热"
        if failed:
            logger.warning("缓存预热完成(含失败): 成功 %d, 失败 %d/%d, 失败键=%s",
                           warmed, len(failed), len(loaders), failed[:10])
        else:
            logger.info(f"缓存预热完成: {warmed}/{len(loaders)} 项")
        return warmed

    def get_stats(self) -> Dict[str, Any]:
        l2_stats = self.l2.get_stats()
        return {
            'l1': self.l1.get_stats(),
            'l2': l2_stats,
            # 把 L2 故障计数提到顶层，方便监控一眼看到"缓存层在报错"
            'l2_errors': dict(self.l2._errors),
            'l2_healthy': l2_stats.get('available', False),
        }

    def cleanup(self) -> int:
        """清理过期缓存"""
        return self.l2.cleanup_expired()


# 全局实例
tiered_cache = TieredCache()
