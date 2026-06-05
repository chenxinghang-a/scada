"""
数据库索引优化建议
分析查询日志和表结构，提供缺失索引建议。

使用方式:
    from core.index_advisor import IndexAdvisor
    advisor = IndexAdvisor(db)
    suggestions = advisor.analyze()
"""

import re
import sqlite3
import logging
import threading
from typing import List, Dict, Any, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


def _safe_identifier(name: str) -> str:
    """验证并转义SQL标识符（表名/列名）"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid identifier: {name}")
    return '"' + name.replace('"', '""') + '"'


class IndexAdvisor:
    """索引优化顾问"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._query_log: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def log_query(self, sql: str, duration_ms: float, table: str = ''):
        """记录查询用于分析"""
        with self._lock:
            self._query_log.append({
                'sql': sql[:500],
                'duration_ms': duration_ms,
                'table': table,
            })
            # 保留最近1000条
            if len(self._query_log) > 1000:
                self._query_log = self._query_log[-1000:]

    def analyze(self) -> Dict[str, Any]:
        """分析并提供索引建议"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row

            result = {
                'existing_indexes': self._get_existing_indexes(conn),
                'missing_indexes': self._suggest_missing_indexes(conn),
                'unused_indexes': self._find_unused_indexes(conn),
                'table_stats': self._get_table_stats(conn),
                'recommendations': [],
            }

            # 生成建议
            result['recommendations'] = self._generate_recommendations(result)

            conn.close()
            return result
        except Exception as e:
            logger.error("索引分析失败: %s", e)
            return {'error': str(e)}

    def _get_existing_indexes(self, conn: sqlite3.Connection) -> List[Dict]:
        """获取现有索引"""
        cursor = conn.execute("""
            SELECT name, tbl_name, sql
            FROM sqlite_master
            WHERE type='index' AND sql IS NOT NULL
            ORDER BY tbl_name, name
        """)
        return [dict(row) for row in cursor.fetchall()]

    def _suggest_missing_indexes(self, conn: sqlite3.Connection) -> List[Dict]:
        """建议缺失的索引"""
        suggestions = []

        # 分析查询日志中的WHERE条件
        with self._lock:
            queries = list(self._query_log)

        # 统计各表的查询频率
        table_queries = defaultdict(list)
        for q in queries:
            if q['table']:
                table_queries[q['table']].append(q)

        # 检查每个表
        cursor = conn.execute("""
            SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'
        """)
        tables = [row['name'] for row in cursor.fetchall()]

        for table in tables:
            safe_table = _safe_identifier(table)
            # 获取现有索引列
            existing_cols = set()
            idx_sql = "PRAGMA index_list(" + safe_table + ")"
            idx_cursor = conn.execute(idx_sql)
            for idx in idx_cursor.fetchall():
                safe_idx = _safe_identifier(idx['name'])
                col_sql = "PRAGMA index_info(" + safe_idx + ")"
                col_cursor = conn.execute(col_sql)
                for col in col_cursor.fetchall():
                    existing_cols.add(col['name'])

            # 获取列信息
            table_info_sql = "PRAGMA table_info(" + safe_table + ")"
            col_cursor = conn.execute(table_info_sql)
            columns = [row['name'] for row in col_cursor.fetchall()]

            # 分析查询模式
            for q in table_queries.get(table, []):
                sql = q['sql'].upper()
                if 'WHERE' in sql:
                    for col in columns:
                        if col.upper() in sql and col not in existing_cols:
                            safe_col = _safe_identifier(col)
                            create_sql = (
                                'CREATE INDEX IF NOT EXISTS idx_' + table + '_' + col +
                                ' ON ' + safe_table + '(' + safe_col + ')'
                            )
                            suggestions.append({
                                'table': table,
                                'column': col,
                                'reason': 'WHERE条件中使用但无索引',
                                'sql': create_sql,
                                'priority': 'high' if q['duration_ms'] > 100 else 'medium',
                            })

        return suggestions

    def _find_unused_indexes(self, conn: sqlite3.Connection) -> List[Dict]:
        """查找未使用的索引"""
        unused = []
        cursor = conn.execute("""
            SELECT name, tbl_name, sql
            FROM sqlite_master
            WHERE type='index' AND sql IS NOT NULL
            AND name NOT LIKE 'sqlite_%'
            AND name NOT LIKE '%_pkey'
            AND name NOT LIKE '%_unique'
        """)

        for idx in cursor.fetchall():
            # 检查索引是否在查询日志中被使用
            used = False
            with self._lock:
                for q in self._query_log:
                    if idx['name'].upper() in q['sql'].upper():
                        used = True
                        break
            if not used and len(self._query_log) > 50:
                unused.append({
                    'index': idx['name'],
                    'table': idx['tbl_name'],
                    'sql': idx['sql'],
                })

        return unused

    def _get_table_stats(self, conn: sqlite3.Connection) -> List[Dict]:
        """获取表统计信息"""
        stats = []
        cursor = conn.execute("""
            SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'
        """)
        tables = [row['name'] for row in cursor.fetchall()]

        for table in tables:
            safe_table = _safe_identifier(table)
            try:
                count_sql = 'SELECT COUNT(*) FROM ' + safe_table
                count = conn.execute(count_sql).fetchone()[0]
                # 获取页数和大小
                page_count = conn.execute('PRAGMA page_count').fetchone()[0]
                page_size = conn.execute('PRAGMA page_size').fetchone()[0]

                stats.append({
                    'table': table,
                    'row_count': count,
                    'estimated_size_kb': page_count * page_size / 1024,
                })
            except Exception as e:
                stats.append({'table': table, 'error': str(e)})

        return stats

    def _generate_recommendations(self, analysis: Dict) -> List[str]:
        """生成优化建议"""
        recs = []

        if analysis.get('missing_indexes'):
            high = [s for s in analysis['missing_indexes'] if s['priority'] == 'high']
            if high:
                recs.append(f"发现 {len(high)} 个高优先级缺失索引建议")

        if analysis.get('unused_indexes'):
            recs.append(f"发现 {len(analysis['unused_indexes'])} 个可能未使用的索引")

        for stat in analysis.get('table_stats', []):
            if stat.get('row_count', 0) > 100000:
                recs.append(f"表 {stat['table']} 有 {stat['row_count']} 行，建议添加分页索引")

        return recs
