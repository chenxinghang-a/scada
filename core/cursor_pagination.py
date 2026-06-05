"""
游标分页支持
大数据集高效分页，避免OFFSET性能问题。

使用方式:
    from core.cursor_pagination import CursorPaginator
    paginator = CursorPaginator(db)
    result = paginator.paginate('history_data', cursor='abc123', limit=20)
"""

import base64
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class CursorPaginator:
    """游标分页器"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def encode_cursor(self, values: Dict[str, Any]) -> str:
        """编码游标"""
        cursor_data = json.dumps(values, default=str)
        return base64.urlsafe_b64encode(cursor_data.encode()).decode()

    def decode_cursor(self, cursor: str) -> Dict[str, Any]:
        """解码游标"""
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode())
            return json.loads(decoded)
        except Exception as e:
            raise ValueError(f"无效的游标: {e}")

    def paginate(
        self,
        table: str,
        cursor: Optional[str] = None,
        limit: int = 20,
        order_by: str = 'id',
        order_dir: str = 'ASC',
        filters: Optional[Dict[str, Any]] = None,
        columns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        游标分页查询

        Args:
            table: 表名
            cursor: 游标（None表示第一页）
            limit: 每页数量
            order_by: 排序字段
            order_dir: 排序方向（ASC/DESC）
            filters: 过滤条件
            columns: 返回列（None表示全部）

        Returns:
            {
                'data': [...],
                'next_cursor': 'xxx' or None,
                'prev_cursor': 'xxx' or None,
                'has_more': bool,
            }
        """
        # 安全校验表名和列名
        if not table.isalnum() and '_' not in table:
            raise ValueError(f"无效的表名: {table}")
        if not order_by.isalnum() and '_' not in order_by:
            raise ValueError(f"无效的排序字段: {order_by}")
        if order_dir.upper() not in ('ASC', 'DESC'):
            raise ValueError(f"无效的排序方向: {order_dir}")

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            # 构建查询
            cols = ', '.join(columns) if columns else '*'
            sql = f'SELECT {cols} FROM "{table}"'
            params: List[Any] = []

            # 过滤条件
            if filters:
                where_clauses = []
                for key, value in filters.items():
                    if not key.isalnum() and '_' not in key:
                        continue
                    where_clauses.append(f'"{key}" = ?')
                    params.append(value)
                if where_clauses:
                    sql += ' WHERE ' + ' AND '.join(where_clauses)

            # 游标条件
            if cursor:
                cursor_data = self.decode_cursor(cursor)
                cursor_value = cursor_data.get(order_by)
                if cursor_value is not None:
                    operator = '>' if order_dir.upper() == 'ASC' else '<'
                    if 'WHERE' in sql:
                        sql += f' AND "{order_by}" {operator} ?'
                    else:
                        sql += f' WHERE "{order_by}" {operator} ?'
                    params.append(cursor_value)

            # 排序和限制
            sql += f' ORDER BY "{order_by}" {order_dir}'
            sql += f' LIMIT {min(limit + 1, 100)}'  # 多查1条判断是否有下一页

            # 执行查询
            cursor_result = conn.execute(sql, params)
            rows = cursor_result.fetchall()

            # 判断是否有下一页
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]

            # 转换为字典
            data = [dict(row) for row in rows]

            # 生成游标
            next_cursor = None
            prev_cursor = None

            if data:
                if has_more:
                    last_row = data[-1]
                    next_cursor = self.encode_cursor({order_by: last_row.get(order_by)})

                if cursor:
                    first_row = data[0]
                    prev_cursor = self.encode_cursor({order_by: first_row.get(order_by)})

            return {
                'data': data,
                'next_cursor': next_cursor,
                'prev_cursor': prev_cursor,
                'has_more': has_more,
                'count': len(data),
            }

        finally:
            conn.close()

    def get_total_count(self, table: str, filters: Optional[Dict[str, Any]] = None) -> int:
        """获取总数"""
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            sql = f'SELECT COUNT(*) FROM "{table}"'
            params: List[Any] = []

            if filters:
                where_clauses = []
                for key, value in filters.items():
                    if not key.isalnum() and '_' not in key:
                        continue
                    where_clauses.append(f'"{key}" = ?')
                    params.append(value)
                if where_clauses:
                    sql += ' WHERE ' + ' AND '.join(where_clauses)

            result = conn.execute(sql, params).fetchone()
            return result[0] if result else 0
        finally:
            conn.close()


def create_cursor_response(
    data: List[Dict],
    next_cursor: Optional[str],
    has_more: bool,
    total: Optional[int] = None,
) -> Dict[str, Any]:
    """创建游标分页响应"""
    response = {
        'success': True,
        'data': data,
        'pagination': {
            'next_cursor': next_cursor,
            'has_more': has_more,
            'count': len(data),
        },
    }
    if total is not None:
        response['pagination']['total'] = total
    return response
