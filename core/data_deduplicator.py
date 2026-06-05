"""
数据去重支持
重复数据检测、合并、清理。

使用方式:
    from core.data_deduplicator import DataDeduplicator
    deduplicator = DataDeduplicator(db)
    duplicates = deduplicator.find_duplicates('history_data', ['device_id', 'register_name', 'timestamp'])
"""

import logging
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DataDeduplicator:
    """数据去重器"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def find_duplicates(
        self,
        table: str,
        key_columns: List[str],
        time_window_seconds: int = 0,
    ) -> Dict[str, Any]:
        """
        查找重复数据

        Args:
            table: 表名
            key_columns: 用于判断重复的列
            time_window_seconds: 时间窗口（秒），0表示精确匹配

        Returns:
            {
                'total_duplicates': int,
                'duplicate_groups': [...],
                'sample': [...],
            }
        """
        # 安全校验
        if not table.isalnum() and '_' not in table:
            raise ValueError(f"无效的表名: {table}")

        for col in key_columns:
            if not col.isalnum() and '_' not in col:
                raise ValueError(f"无效的列名: {col}")

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            # 构建分组查询
            cols = ', '.join(f'"{c}"' for c in key_columns)
            sql = f'''
                SELECT {cols}, COUNT(*) as cnt
                FROM "{table}"
                GROUP BY {cols}
                HAVING cnt > 1
                ORDER BY cnt DESC
                LIMIT 100
            '''

            cursor = conn.execute(sql)
            groups = [dict(row) for row in cursor.fetchall()]

            # 获取重复详情
            duplicate_groups = []
            total_duplicates = 0

            for group in groups:
                cnt = group['cnt']
                total_duplicates += cnt - 1  # 每组保留1条，其余为重复

                # 获取该组的ID列表
                where_clauses = []
                params = []
                for col in key_columns:
                    where_clauses.append(f'"{col}" = ?')
                    params.append(group[col])

                ids_sql = f'''
                    SELECT id FROM "{table}"
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY id
                '''
                ids_cursor = conn.execute(ids_sql, params)
                ids = [row['id'] for row in ids_cursor.fetchall()]

                duplicate_groups.append({
                    'key': {col: group[col] for col in key_columns},
                    'count': cnt,
                    'ids': ids,
                    'keep_id': ids[0],  # 保留第一条
                    'remove_ids': ids[1:],  # 删除其余
                })

            return {
                'total_duplicates': total_duplicates,
                'duplicate_groups': duplicate_groups[:50],  # 限制返回数量
                'group_count': len(duplicate_groups),
            }

        finally:
            conn.close()

    def remove_duplicates(
        self,
        table: str,
        key_columns: List[str],
        keep_strategy: str = 'first',
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """
        删除重复数据

        Args:
            table: 表名
            key_columns: 用于判断重复的列
            keep_strategy: 保留策略（first/last/newest/oldest）
            dry_run: 仅预览不实际删除

        Returns:
            {
                'removed_count': int,
                'kept_count': int,
                'dry_run': bool,
            }
        """
        # 先查找重复
        duplicates = self.find_duplicates(table, key_columns)

        if duplicates['total_duplicates'] == 0:
            return {
                'removed_count': 0,
                'kept_count': 0,
                'dry_run': dry_run,
                'message': '没有发现重复数据',
            }

        if dry_run:
            return {
                'removed_count': duplicates['total_duplicates'],
                'kept_count': duplicates['group_count'],
                'dry_run': True,
                'duplicate_groups': duplicates['duplicate_groups'][:10],
            }

        # 实际删除
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            removed_count = 0

            for group in duplicates['duplicate_groups']:
                # 根据策略选择保留的ID
                if keep_strategy == 'first':
                    keep_id = group['ids'][0]
                elif keep_strategy == 'last':
                    keep_id = group['ids'][-1]
                elif keep_strategy == 'newest':
                    # 假设有timestamp字段
                    keep_id = group['ids'][0]  # 默认保留第一条
                else:
                    keep_id = group['ids'][0]

                # 删除除保留ID外的所有记录
                remove_ids = [id for id in group['ids'] if id != keep_id]
                if remove_ids:
                    placeholders = ','.join(['?' for _ in remove_ids])
                    sql = 'DELETE FROM "' + table + '" WHERE id IN (' + placeholders + ')'
                    conn.execute(sql, remove_ids)
                    removed_count += len(remove_ids)

            conn.commit()

            return {
                'removed_count': removed_count,
                'kept_count': duplicates['group_count'],
                'dry_run': False,
            }

        finally:
            conn.close()

    def find_similar_records(
        self,
        table: str,
        column: str,
        similarity_threshold: float = 0.8,
    ) -> List[Dict[str, Any]]:
        """
        查找相似记录（基于字符串相似度）

        Args:
            table: 表名
            column: 比较列
            similarity_threshold: 相似度阈值（0-1）

        Returns:
            相似记录对列表
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            # 获取所有记录
            sql = 'SELECT id, "' + column + '" FROM "' + table + '" WHERE "' + column + '" IS NOT NULL'
            cursor = conn.execute(sql)
            records = [dict(row) for row in cursor.fetchall()]

            # 查找相似对
            similar_pairs = []

            for i in range(len(records)):
                for j in range(i + 1, min(len(records), i + 100)):  # 限制比较范围
                    text1 = str(records[i].get(column, ''))
                    text2 = str(records[j].get(column, ''))

                    if not text1 or not text2:
                        continue

                    similarity = self._calculate_similarity(text1, text2)

                    if similarity >= similarity_threshold:
                        similar_pairs.append({
                            'record1': records[i],
                            'record2': records[j],
                            'similarity': round(similarity, 3),
                        })

            return similar_pairs[:100]  # 限制返回数量

        finally:
            conn.close()

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算字符串相似度（Jaccard系数）"""
        set1 = set(text1.lower().split())
        set2 = set(text2.lower().split())

        if not set1 and not set2:
            return 1.0

        intersection = set1 & set2
        union = set1 | set2

        return len(intersection) / len(union) if union else 0.0

    def get_dedup_stats(self, table: str) -> Dict[str, Any]:
        """获取去重统计"""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            # 总记录数
            total = conn.execute('SELECT COUNT(*) as cnt FROM "' + table + '"').fetchone()['cnt']

            # 获取列信息
            columns = conn.execute('PRAGMA table_info("' + table + '")').fetchall()
            col_names = [col['name'] for col in columns if col['name'] != 'id']

            return {
                'table': table,
                'total_records': total,
                'columns': col_names,
            }

        finally:
            conn.close()


def create_dedup_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """创建去重响应"""
    return {
        'success': True,
        **result,
    }
