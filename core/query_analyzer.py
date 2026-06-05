"""
数据库查询分析器
分析查询性能，识别慢查询和优化建议。

使用方式:
    from core.query_analyzer import QueryAnalyzer
    analyzer = QueryAnalyzer(db_path)
    report = analyzer.analyze_query("SELECT * FROM devices WHERE status = ?", ('online',))
"""

import time
import sqlite3
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class QueryStats:
    """查询统计"""
    query: str
    execution_time: float
    rows_affected: int
    timestamp: float
    success: bool
    error: Optional[str] = None


class QueryAnalyzer:
    """查询分析器"""

    def __init__(self, db_path: str, slow_query_threshold: float = 1.0):
        """
        Args:
            db_path: 数据库路径
            slow_query_threshold: 慢查询阈值（秒）
        """
        self.db_path = db_path
        self.slow_query_threshold = slow_query_threshold
        self._query_history: List[QueryStats] = []
        self._lock = threading.Lock()
        self._max_history = 1000

    def analyze_query(self, query: str, params: tuple = None) -> Dict[str, Any]:
        """
        分析单个查询的执行计划

        Args:
            query: SQL查询
            params: 查询参数

        Returns:
            分析结果
        """
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row

            # 获取执行计划
            explain_query = f"EXPLAIN QUERY PLAN {query}"
            cursor = conn.execute(explain_query, params or ())
            plan = [dict(row) for row in cursor.fetchall()]

            # 测量执行时间
            start_time = time.time()
            cursor = conn.execute(query, params or ())
            rows = cursor.fetchall()
            execution_time = time.time() - start_time

            conn.close()

            # 分析结果
            result = {
                'query': query[:200],
                'execution_time': round(execution_time * 1000, 2),  # ms
                'row_count': len(rows),
                'plan': plan,
                'is_slow': execution_time > self.slow_query_threshold,
                'suggestions': self._generate_suggestions(query, plan, execution_time),
            }

            # 记录统计
            self._record_stats(query, execution_time, len(rows), True)

            return result

        except Exception as e:
            self._record_stats(query, 0, 0, False, str(e))
            return {
                'query': query[:200],
                'error': str(e),
                'execution_time': 0,
                'row_count': 0,
                'plan': [],
                'is_slow': False,
                'suggestions': [],
            }

    def _generate_suggestions(self, query: str, plan: List[Dict], execution_time: float) -> List[str]:
        """生成优化建议"""
        suggestions = []
        query_upper = query.upper()

        # 检查是否有全表扫描
        for p in plan:
            detail = str(p.get('detail', '')).upper()
            if 'SCAN' in detail and 'INDEX' not in detail:
                suggestions.append("检测到全表扫描，建议添加索引")

        # 检查SELECT *
        if 'SELECT *' in query_upper:
            suggestions.append("避免使用SELECT *，只查询需要的字段")

        # 检查LIKE前缀通配符
        if "LIKE '%'" in query_upper:
            suggestions.append("前缀通配符(%)会导致索引失效")

        # 检查OR条件
        if ' OR ' in query_upper:
            suggestions.append("OR条件可能导致索引失效，考虑使用UNION")

        # 检查子查询
        if 'SELECT' in query_upper and query_upper.count('SELECT') > 1:
            suggestions.append("检测到子查询，考虑使用JOIN优化")

        # 检查执行时间
        if execution_time > self.slow_query_threshold:
            suggestions.append(f"查询执行时间({execution_time:.2f}s)超过阈值({self.slow_query_threshold}s)")

        # 检查是否缺少WHERE
        if 'SELECT' in query_upper and 'WHERE' not in query_upper and 'LIMIT' not in query_upper:
            suggestions.append("查询缺少WHERE和LIMIT，可能返回大量数据")

        return suggestions

    def _record_stats(self, query: str, execution_time: float, rows: int, success: bool, error: str = None):
        """记录查询统计"""
        stats = QueryStats(
            query=query[:200],
            execution_time=execution_time,
            rows_affected=rows,
            timestamp=time.time(),
            success=success,
            error=error,
        )

        with self._lock:
            self._query_history.append(stats)
            if len(self._query_history) > self._max_history:
                self._query_history = self._query_history[-self._max_history:]

    def get_slow_queries(self, limit: int = 20) -> List[Dict[str, Any]]:
        """获取慢查询列表"""
        with self._lock:
            slow_queries = [
                {
                    'query': s.query,
                    'execution_time': round(s.execution_time * 1000, 2),
                    'rows_affected': s.rows_affected,
                    'timestamp': datetime.fromtimestamp(s.timestamp).isoformat(),
                }
                for s in self._query_history
                if s.execution_time > self.slow_query_threshold
            ]
            slow_queries.sort(key=lambda x: x['execution_time'], reverse=True)
            return slow_queries[:limit]

    def get_query_stats(self) -> Dict[str, Any]:
        """获取查询统计"""
        with self._lock:
            if not self._query_history:
                return {
                    'total_queries': 0,
                    'slow_queries': 0,
                    'avg_execution_time': 0,
                    'max_execution_time': 0,
                }

            total = len(self._query_history)
            slow = sum(1 for s in self._query_history if s.execution_time > self.slow_query_threshold)
            avg_time = sum(s.execution_time for s in self._query_history) / total
            max_time = max(s.execution_time for s in self._query_history)

            return {
                'total_queries': total,
                'slow_queries': slow,
                'slow_query_rate': round(slow / total * 100, 2),
                'avg_execution_time': round(avg_time * 1000, 2),
                'max_execution_time': round(max_time * 1000, 2),
                'slow_query_threshold': round(self.slow_query_threshold * 1000, 2),
            }

    def _validate_identifier(self, name: str) -> str:
        """验证SQL标识符安全性（只允许字母、数字、下划线）"""
        import re
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
            raise ValueError(f"不安全的SQL标识符: {name}")
        return name

    def analyze_table(self, table_name: str) -> Dict[str, Any]:
        """分析表结构和索引"""
        try:
            # 验证表名安全性
            safe_table = self._validate_identifier(table_name)

            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row

            # 获取表信息（PRAGMA不支持参数化，但表名已验证）
            cursor = conn.execute("PRAGMA table_info('{}')".format(safe_table))
            columns = [dict(row) for row in cursor.fetchall()]

            # 获取索引信息
            cursor = conn.execute("PRAGMA index_list('{}')".format(safe_table))
            indexes = [dict(row) for row in cursor.fetchall()]

            # 获取索引详情
            index_details = []
            for idx in indexes:
                idx_name = self._validate_identifier(idx['name'])
                cursor = conn.execute("PRAGMA index_info('{}')".format(idx_name))
                idx_columns = [dict(row) for row in cursor.fetchall()]
                index_details.append({
                    'name': idx_name,
                    'unique': idx.get('unique', 0),
                    'columns': [col['name'] for col in idx_columns],
                })

            # 获取行数估算（参数化查询）
            cursor = conn.execute("SELECT COUNT(*) as count FROM \"{}\"".format(safe_table))
            row_count = cursor.fetchone()['count']

            conn.close()

            return {
                'table': table_name,
                'columns': columns,
                'indexes': index_details,
                'row_count': row_count,
                'suggestions': self._table_suggestions(columns, index_details, row_count),
            }

        except Exception as e:
            return {
                'table': table_name,
                'error': str(e),
                'columns': [],
                'indexes': [],
                'row_count': 0,
                'suggestions': [],
            }

    def _table_suggestions(self, columns: List[Dict], indexes: List[Dict], row_count: int) -> List[str]:
        """生成表优化建议"""
        suggestions = []

        # 检查是否有主键
        has_primary_key = any(idx.get('unique') for idx in indexes)
        if not has_primary_key:
            suggestions.append("表缺少主键索引")

        # 检查外键列是否有索引
        indexed_columns = set()
        for idx in indexes:
            for col in idx.get('columns', []):
                indexed_columns.add(col)

        # 检查常用查询字段是否有索引
        common_fields = ['device_id', 'register_name', 'timestamp', 'status', 'type']
        for field in common_fields:
            if field in [col['name'] for col in columns] and field not in indexed_columns:
                suggestions.append(f"常用字段 '{field}' 缺少索引")

        # 检查行数
        if row_count > 100000:
            suggestions.append(f"表有{row_count}行，考虑分区或归档")

        return suggestions


# 全局实例
_query_analyzer: Optional[QueryAnalyzer] = None


def get_query_analyzer(db_path: str = None) -> QueryAnalyzer:
    """获取查询分析器实例"""
    global _query_analyzer
    if _query_analyzer is None or (db_path and _query_analyzer.db_path != db_path):
        _query_analyzer = QueryAnalyzer(db_path or 'data/scada.db')
    return _query_analyzer
