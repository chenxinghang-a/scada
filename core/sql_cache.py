"""
SQL查询结果缓存
缓存频繁查询的SQL结果，减少数据库负载。

增强特性:
- 表级失效：写入某表时自动失效相关查询缓存
- 模式匹配失效：按SQL模式批量失效
- 缓存预热：启动时预加载常用查询
- 访问统计：命中率/LRU/按表统计

使用方式:
    from core.sql_cache import sql_cache
    result = sql_cache.get_or_execute('SELECT * FROM devices', conn.execute, ttl=60)
    sql_cache.invalidate_table('devices')  # 写入后失效
"""

import hashlib
import time
import threading
import logging
from typing import Any, Callable, Dict, Optional, Tuple
from functools import wraps

logger = logging.getLogger(__name__)


class SQLCacheEntry:
    """SQL缓存条目"""

    def __init__(self, key: str, result: Any, ttl: float, tables: List[str] = None):
        self.key = key
        self.result = result
        self.ttl = ttl
        self.tables = tables or []  # 涉及的表名
        self.created_at = time.time()
        self.access_count = 0
        self.last_access = self.created_at

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl

    def get(self) -> Any:
        self.access_count += 1
        self.last_access = time.time()
        return self.result


def _extract_tables_from_sql(sql: str) -> List[str]:
    """从SQL语句中提取表名"""
    import re
    # 匹配 FROM/JOIN/INTO/UPDATE 后的表名
    patterns = [
        r'(?:FROM|JOIN)\s+["\']?(\w+)["\']?',
        r'(?:INTO|UPDATE)\s+["\']?(\w+)["\']?',
    ]
    tables = set()
    for pattern in patterns:
        for match in re.finditer(pattern, sql, re.IGNORECASE):
            table = match.group(1).strip('"').strip("'")
            if not table.startswith('sqlite_'):
                tables.add(table.lower())
    return list(tables)


class SQLCache:
    """SQL查询结果缓存"""

    def __init__(
        self,
        max_size: int = 1000,
        default_ttl: float = 30.0,
        cleanup_interval: float = 60.0,
    ):
        self._cache: Dict[str, SQLCacheEntry] = {}
        self._lock = threading.Lock()
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()

        # 统计
        self._hits = 0
        self._misses = 0

    def _make_key(self, sql: str, params: tuple = None) -> str:
        """生成缓存键"""
        content = sql
        if params:
            content += str(params)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def get(self, sql: str, params: tuple = None) -> Optional[Any]:
        """获取缓存结果"""
        key = self._make_key(sql, params)

        with self._lock:
            entry = self._cache.get(key)
            if entry and not entry.is_expired:
                self._hits += 1
                return entry.get()

            if entry:
                del self._cache[key]

            self._misses += 1
            return None

    def put(self, sql: str, result: Any, params: tuple = None, ttl: float = None):
        """存储查询结果"""
        key = self._make_key(sql, params)
        ttl = ttl or self._default_ttl
        tables = _extract_tables_from_sql(sql)

        with self._lock:
            # 容量检查
            if len(self._cache) >= self._max_size:
                self._evict()

            self._cache[key] = SQLCacheEntry(key, result, ttl, tables)

    def invalidate_table(self, table: str):
        """使表相关的所有缓存失效（写入后调用）"""
        table_lower = table.lower()
        with self._lock:
            keys_to_remove = [
                key for key, entry in self._cache.items()
                if table_lower in entry.tables
            ]
            for key in keys_to_remove:
                del self._cache[key]
            if keys_to_remove:
                logger.debug(f"表级缓存失效: {table} ({len(keys_to_remove)}条)")

    def invalidate_pattern(self, pattern: str):
        """按SQL模式批量失效"""
        with self._lock:
            keys_to_remove = [
                key for key, entry in self._cache.items()
                if pattern.lower() in entry.key
            ]
            for key in keys_to_remove:
                del self._cache[key]

    def warmup(self, queries: List[Tuple[str, Callable, tuple]]):
        """缓存预热"""
        for sql, executor, params in queries:
            try:
                self.get_or_execute(sql, executor, params)
            except Exception as e:
                logger.warning(f"缓存预热失败: {e}")

    def get_stats_by_table(self) -> Dict[str, Dict[str, Any]]:
        """按表统计缓存命中"""
        with self._lock:
            table_stats = {}
            for entry in self._cache.values():
                for table in entry.tables:
                    if table not in table_stats:
                        table_stats[table] = {'count': 0, 'total_access': 0}
                    table_stats[table]['count'] += 1
                    table_stats[table]['total_access'] += entry.access_count
            return table_stats

    def get_or_execute(
        self,
        sql: str,
        executor: Callable,
        params: tuple = None,
        ttl: float = None,
    ) -> Any:
        """获取缓存或执行查询"""
        cached = self.get(sql, params)
        if cached is not None:
            return cached

        result = executor(sql, params) if params else executor(sql)
        self.put(sql, result, params, ttl)
        return result

    def invalidate(self, sql: str, params: tuple = None):
        """使缓存失效"""
        key = self._make_key(sql, params)
        with self._lock:
            self._cache.pop(key, None)

    def invalidate_prefix(self, table: str):
        """使表相关的缓存失效"""
        with self._lock:
            keys_to_remove = []
            for key, entry in self._cache.items():
                if table.lower() in key.lower():
                    keys_to_remove.append(key)
            for key in keys_to_remove:
                del self._cache[key]

    def clear(self):
        """清空缓存"""
        with self._lock:
            self._cache.clear()

    def _evict(self):
        """淘汰最旧的条目"""
        if not self._cache:
            return

        # 先淘汰过期的
        expired = [k for k, v in self._cache.items() if v.is_expired]
        for k in expired:
            del self._cache[k]

        # 如果还需要空间，淘汰最少访问的
        if len(self._cache) >= self._max_size:
            sorted_entries = sorted(
                self._cache.items(),
                key=lambda x: x[1].access_count
            )
            for key, _ in sorted_entries[:len(sorted_entries) // 4]:
                del self._cache[key]

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        with self._lock:
            total = self._hits + self._misses
            return {
                'size': len(self._cache),
                'max_size': self._max_size,
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': round(self._hits / total * 100, 2) if total > 0 else 0,
                'default_ttl': self._default_ttl,
            }


# 全局实例
sql_cache = SQLCache()


def cached_query(ttl: float = 30.0):
    """
    SQL查询缓存装饰器

    Args:
        ttl: 缓存TTL（秒）
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            # 生成缓存键
            key_parts = [f.__name__] + [str(a) for a in args] + [f"{k}={v}" for k, v in kwargs.items()]
            cache_key = '|'.join(key_parts)

            cached = sql_cache.get(cache_key)
            if cached is not None:
                return cached

            result = f(*args, **kwargs)
            sql_cache.put(cache_key, result, ttl=ttl)
            return result
        return decorated
    return decorator
