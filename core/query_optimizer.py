"""
数据库查询优化器
慢查询自动检测+重写建议+执行计划分析。

使用方式:
    from core.query_optimizer import QueryOptimizer
    optimizer = QueryOptimizer(db_path)
    result = optimizer.analyze_query("SELECT * FROM devices WHERE name LIKE '%pump%'")
    print(result.suggestions)
"""

import re
import time
import sqlite3
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class QueryAnalysis:
    """查询分析结果"""
    original_sql: str
    optimized_sql: Optional[str] = None
    execution_time_ms: float = 0.0
    suggestions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    uses_index: bool = False
    scan_type: str = 'unknown'  # full_scan, index_scan, index_seek
    estimated_rows: int = 0


class QueryOptimizer:
    """查询优化器"""

    # 慢查询阈值（毫秒）
    SLOW_QUERY_THRESHOLD_MS = 100

    # 查询模式匹配
    PATTERNS = {
        'select_star': (r'SELECT\s+\*\s+FROM', '避免SELECT *，只查询需要的列'),
        'like_prefix_wildcard': (r"LIKE\s+'%", '前缀通配符会导致全表扫描，考虑使用全文索引'),
        'or_condition': (r'\bOR\b', 'OR条件可能导致索引失效，考虑使用UNION ALL'),
        'not_in': (r'\bNOT\s+IN\b', 'NOT IN性能较差，考虑使用LEFT JOIN + IS NULL'),
        'subquery_in': (r'IN\s*\(SELECT', '子查询性能较差，考虑使用JOIN'),
        'function_on_column': (r'WHERE\s+\w+\(', '对列使用函数会导致索引失效'),
        'implicit_conversion': (r"WHERE\s+\w+\s*=\s*'?\d+'?", '隐式类型转换可能导致索引失效'),
        'missing_limit': (r'SELECT\s+.*\s+FROM\s+(?!.*\bLIMIT\b)', '查询缺少LIMIT限制，可能导致返回大量数据'),
    }

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._slow_queries: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stats = {
            'total_queries': 0,
            'slow_queries': 0,
            'rewritten_queries': 0,
        }

    def analyze_query(self, sql: str, params: Optional[tuple] = None) -> QueryAnalysis:
        """分析单个查询"""
        analysis = QueryAnalysis(original_sql=sql)

        # 1. 模式匹配检查
        for pattern_name, (pattern, suggestion) in self.PATTERNS.items():
            if re.search(pattern, sql, re.IGNORECASE):
                analysis.suggestions.append(suggestion)
                analysis.warnings.append(f'检测到{pattern_name}模式')

        # 2. 执行EXPLAIN QUERY PLAN
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cursor = conn.execute(f"EXPLAIN QUERY PLAN {sql}")
            plan = cursor.fetchall()

            # 分析执行计划
            plan_text = ' '.join(str(row) for row in plan)
            if 'SCAN TABLE' in plan_text:
                analysis.scan_type = 'full_scan'
                analysis.suggestions.append('检测到全表扫描，建议添加索引')
                analysis.uses_index = False
            elif 'SEARCH TABLE' in plan_text:
                analysis.scan_type = 'index_scan'
                analysis.uses_index = True

            conn.close()
        except Exception as e:
            analysis.warnings.append(f'EXPLAIN执行失败: {e}')

        # 3. 生成优化建议
        optimized = self._try_rewrite(sql)
        if optimized != sql:
            analysis.optimized_sql = optimized
            analysis.suggestions.append(f'建议重写为: {optimized[:100]}...')

        return analysis

    def _try_rewrite(self, sql: str) -> str:
        """尝试自动重写查询"""
        rewritten = sql

        # SELECT * → 建议列出具体列
        if re.search(r'SELECT\s+\*', sql, re.IGNORECASE):
            rewritten = re.sub(
                r'SELECT\s+\*',
                'SELECT /* TODO: 列出具体列名 */ *',
                rewritten,
                flags=re.IGNORECASE
            )

        # LIKE '%xxx%' → 建议使用全文索引
        if re.search(r"LIKE\s+'%", sql, re.IGNORECASE):
            rewritten = re.sub(
                r"LIKE\s+'(%[^']+%)'",
                r"LIKE \1  -- TODO: 考虑使用全文索引",
                rewritten,
                flags=re.IGNORECASE
            )

        return rewritten

    def wrap_cursor(self, cursor: sqlite3.Cursor) -> 'MonitoredCursor':
        """包装游标以监控查询"""
        return MonitoredCursor(cursor, self)

    def record_slow_query(self, sql: str, duration_ms: float, params: Optional[tuple] = None):
        """记录慢查询"""
        with self._lock:
            self._stats['total_queries'] += 1

            if duration_ms > self.SLOW_QUERY_THRESHOLD_MS:
                self._stats['slow_queries'] += 1
                self._slow_queries.append({
                    'sql': sql[:500],
                    'params': str(params)[:200] if params else None,
                    'duration_ms': round(duration_ms, 2),
                    'timestamp': datetime.now().isoformat(),
                })

                # 保留最近100条
                if len(self._slow_queries) > 100:
                    self._slow_queries = self._slow_queries[-100:]

                logger.warning(
                    "慢查询: %.1fms | %s",
                    duration_ms, sql[:200]
                )

    def get_slow_queries(self, limit: int = 20) -> List[Dict[str, Any]]:
        """获取最近的慢查询"""
        with self._lock:
            return list(reversed(self._slow_queries[-limit:]))

    def get_stats(self) -> Dict[str, Any]:
        """获取统计"""
        with self._lock:
            return {
                **self._stats,
                'slow_query_rate': round(
                    self._stats['slow_queries'] / max(1, self._stats['total_queries']) * 100, 2
                ),
            }


class MonitoredCursor:
    """监控游标包装"""

    def __init__(self, cursor: sqlite3.Cursor, optimizer: QueryOptimizer):
        self._cursor = cursor
        self._optimizer = optimizer

    def execute(self, sql: str, params: Optional[tuple] = None) -> 'MonitoredCursor':
        start = time.time()
        try:
            self._cursor.execute(sql, params or ())
        finally:
            duration = (time.time() - start) * 1000
            self._optimizer.record_slow_query(sql, duration, params)
        return self

    def executemany(self, sql: str, params_list: List[tuple]) -> 'MonitoredCursor':
        start = time.time()
        try:
            self._cursor.executemany(sql, params_list)
        finally:
            duration = (time.time() - start) * 1000
            self._optimizer.record_slow_query(sql, duration)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size: int):
        return self._cursor.fetchmany(size)

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        self._cursor.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._cursor.__exit__(*args)


# 全局实例
_query_optimizer: Optional[QueryOptimizer] = None


def get_query_optimizer(db_path: str = None) -> QueryOptimizer:
    """获取查询优化器单例"""
    global _query_optimizer
    if _query_optimizer is None:
        from pathlib import Path
        if db_path is None:
            db_path = str(Path(__file__).parent.parent / 'data' / 'scada.db')
        _query_optimizer = QueryOptimizer(db_path)
    return _query_optimizer
