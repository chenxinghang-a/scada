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
        self._lock = threading.Lock()
        self._init_db()

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
            except Exception:
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
                logger.warning(f"SQLite缓存写入失败: {e}")

    def delete(self, key: str):
        with self._lock:
            try:
                conn = sqlite3.connect(self._db_path, timeout=5)
                conn.execute('DELETE FROM cache WHERE key = ?', (key,))
                conn.commit()
                conn.close()
            except Exception:
                pass

    def cleanup_expired(self) -> int:
        """清理过期缓存"""
        with self._lock:
            try:
                conn = sqlite3.connect(self._db_path, timeout=5)
                cursor = conn.execute('DELETE FROM cache WHERE expires_at < ?', (time.time(),))
                deleted = cursor.rowcount
                conn.commit()
                conn.close()
                return deleted
            except Exception:
                return 0

    def get_stats(self) -> Dict[str, Any]:
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            row = conn.execute('SELECT COUNT(*), SUM(LENGTH(value)) FROM cache').fetchone()
            conn.close()
            return {
                'count': row[0] or 0,
                'size_bytes': row[1] or 0,
            }
        except Exception:
            return {'count': 0, 'size_bytes': 0}


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
        except Exception:
            pass

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
        for key, factory, ttl in loaders:
            try:
                cached = self.get(key)
                if cached is None:
                    value = factory()
                    self.set(key, value, ttl)
                    warmed += 1
            except Exception as e:
                logger.warning(f"缓存预热失败: {key}: {e}")
        logger.info(f"缓存预热完成: {warmed}/{len(loaders)} 项")

    def get_stats(self) -> Dict[str, Any]:
        return {
            'l1': self.l1.get_stats(),
            'l2': self.l2.get_stats(),
        }

    def cleanup(self) -> int:
        """清理过期缓存"""
        return self.l2.cleanup_expired()


# 全局实例
tiered_cache = TieredCache()
