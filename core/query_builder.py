"""
动态查询条件构建器
安全地构建动态SQL WHERE条件，防止SQL注入。

使用方式:
    from core.query_builder import QueryBuilder
    builder = QueryBuilder('devices')
    builder.where('status', '=', 'online')
    builder.where('protocol', 'in', ['modbus_tcp', 'opcua'])
    sql, params = builder.build()
"""

import re
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 允许的操作符
ALLOWED_OPERATORS = {
    '=', '!=', '<>', '>', '<', '>=', '<=',
    'like', 'not like', 'in', 'not in',
    'between', 'is null', 'is not null',
}

# 允许的排序方向
ALLOWED_DIRECTIONS = {'asc', 'desc'}

# 允许的聚合函数
ALLOWED_AGGREGATES = {'count', 'sum', 'avg', 'min', 'max'}


class QueryBuilder:
    """动态查询条件构建器"""

    def __init__(self, table: str):
        """
        Args:
            table: 表名（必须是安全的标识符）
        """
        self._validate_identifier(table)
        self.table = table
        self._conditions: List[str] = []
        self._params: List[Any] = []
        self._order_by: List[str] = []
        self._limit: Optional[int] = None
        self._offset: Optional[int] = None
        self._select_columns: List[str] = ['*']
        self._group_by: List[str] = []
        self._having: List[str] = []
        self._having_params: List[Any] = []
        self._joins: List[str] = []

    def _validate_identifier(self, name: str) -> bool:
        """验证标识符安全性"""
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
            raise ValueError(f"不安全的标识符: {name}")
        return True

    def select(self, *columns: str) -> 'QueryBuilder':
        """选择列"""
        for col in columns:
            if col != '*':
                self._validate_identifier(col)
        self._select_columns = list(columns)
        return self

    def where(self, column: str, operator: str, value: Any = None) -> 'QueryBuilder':
        """
        添加WHERE条件

        Args:
            column: 列名
            operator: 操作符
            value: 值（is null/is not null时不需要）
        """
        self._validate_identifier(column)
        operator = operator.lower().strip()

        if operator not in ALLOWED_OPERATORS:
            raise ValueError(f"不支持的操作符: {operator}")

        if operator in ('is null', 'is not null'):
            self._conditions.append(f'"{column}" {operator}')
        elif operator == 'in':
            if not isinstance(value, (list, tuple)):
                raise ValueError("IN操作符需要列表值")
            placeholders = ', '.join(['?' for _ in value])
            self._conditions.append(f'"{column}" IN ({placeholders})')
            self._params.extend(value)
        elif operator == 'not in':
            if not isinstance(value, (list, tuple)):
                raise ValueError("NOT IN操作符需要列表值")
            placeholders = ', '.join(['?' for _ in value])
            self._conditions.append(f'"{column}" NOT IN ({placeholders})')
            self._params.extend(value)
        elif operator == 'between':
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError("BETWEEN需要两个值")
            self._conditions.append(f'"{column}" BETWEEN ? AND ?')
            self._params.extend(value)
        elif operator in ('like', 'not like'):
            self._conditions.append(f'"{column}" {operator} ?')
            self._params.append(value)
        else:
            self._conditions.append(f'"{column}" {operator} ?')
            self._params.append(value)

        return self

    def where_in_subquery(self, column: str, subquery: str, params: List[Any] = None) -> 'QueryBuilder':
        """子查询条件"""
        self._validate_identifier(column)
        self._conditions.append(f'"{column}" IN ({subquery})')
        if params:
            self._params.extend(params)
        return self

    def where_raw(self, condition: str, params: List[Any] = None) -> 'QueryBuilder':
        """原始条件（谨慎使用）"""
        self._conditions.append(condition)
        if params:
            self._params.extend(params)
        return self

    def order_by(self, column: str, direction: str = 'ASC') -> 'QueryBuilder':
        """排序"""
        self._validate_identifier(column)
        direction = direction.upper().strip()
        if direction not in ALLOWED_DIRECTIONS:
            raise ValueError(f"不支持的排序方向: {direction}")
        self._order_by.append(f'"{column}" {direction}')
        return self

    def limit(self, count: int) -> 'QueryBuilder':
        """限制返回数量"""
        self._limit = max(0, min(count, 10000))  # 最大10000
        return self

    def offset(self, count: int) -> 'QueryBuilder':
        """偏移量"""
        self._offset = max(0, count)
        return self

    def group_by(self, *columns: str) -> 'QueryBuilder':
        """分组"""
        for col in columns:
            self._validate_identifier(col)
            self._group_by.append(f'"{col}"')
        return self

    def having(self, condition: str, params: List[Any] = None) -> 'QueryBuilder':
        """HAVING条件"""
        self._having.append(condition)
        if params:
            self._having_params.extend(params)
        return self

    def join(self, table: str, on_condition: str, join_type: str = 'INNER') -> 'QueryBuilder':
        """JOIN"""
        self._validate_identifier(table)
        join_type = join_type.upper()
        if join_type not in ('INNER', 'LEFT', 'RIGHT', 'FULL'):
            raise ValueError(f"不支持的JOIN类型: {join_type}")
        self._joins.append(f'{join_type} JOIN "{table}" ON {on_condition}')
        return self

    def build(self) -> Tuple[str, List[Any]]:
        """
        构建SQL查询

        Returns:
            (sql, params) 元组
        """
        # SELECT
        columns = ', '.join(self._select_columns)
        sql = f'SELECT {columns} FROM "{self.table}"'

        # JOIN
        if self._joins:
            sql += ' ' + ' '.join(self._joins)

        # WHERE
        params = list(self._params)
        if self._conditions:
            sql += ' WHERE ' + ' AND '.join(self._conditions)

        # GROUP BY
        if self._group_by:
            sql += ' GROUP BY ' + ', '.join(self._group_by)

        # HAVING
        if self._having:
            sql += ' HAVING ' + ' AND '.join(self._having)
            params.extend(self._having_params)

        # ORDER BY
        if self._order_by:
            sql += ' ORDER BY ' + ', '.join(self._order_by)

        # LIMIT
        if self._limit is not None:
            sql += f' LIMIT ?'
            params.append(self._limit)

        # OFFSET
        if self._offset is not None:
            sql += f' OFFSET ?'
            params.append(self._offset)

        return sql, params

    def build_count(self) -> Tuple[str, List[Any]]:
        """构建COUNT查询"""
        sql = f'SELECT COUNT(*) as cnt FROM "{self.table}"'

        if self._joins:
            sql += ' ' + ' '.join(self._joins)

        params = list(self._params)
        if self._conditions:
            sql += ' WHERE ' + ' AND '.join(self._conditions)

        return sql, params

    def build_delete(self) -> Tuple[str, List[Any]]:
        """构建DELETE查询"""
        sql = f'DELETE FROM "{self.table}"'
        params = list(self._params)

        if self._conditions:
            sql += ' WHERE ' + ' AND '.join(self._conditions)

        return sql, params

    def build_update(self, updates: Dict[str, Any]) -> Tuple[str, List[Any]]:
        """构建UPDATE查询"""
        if not updates:
            raise ValueError("更新字段不能为空")

        set_clauses = []
        params = []
        for col, val in updates.items():
            self._validate_identifier(col)
            set_clauses.append(f'"{col}" = ?')
            params.append(val)

        sql = f'UPDATE "{self.table}" SET {", ".join(set_clauses)}'

        if self._conditions:
            sql += ' WHERE ' + ' AND '.join(self._conditions)
            params.extend(self._params)

        return sql, params

    def reset(self) -> 'QueryBuilder':
        """重置构建器"""
        self._conditions.clear()
        self._params.clear()
        self._order_by.clear()
        self._limit = None
        self._offset = None
        self._select_columns = ['*']
        self._group_by.clear()
        self._having.clear()
        self._having_params.clear()
        self._joins.clear()
        return self


def safe_table_name(name: str) -> str:
    """安全的表名引用"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"不安全的表名: {name}")
    return f'"{name}"'


def safe_column_name(name: str) -> str:
    """安全的列名引用"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"不安全的列名: {name}")
    return f'"{name}"'
