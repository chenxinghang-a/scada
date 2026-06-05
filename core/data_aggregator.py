"""
数据聚合查询支持
GROUP BY + 聚合函数，支持多维度统计分析。

使用方式:
    from core.data_aggregator import DataAggregator
    aggregator = DataAggregator(db)
    result = aggregator.aggregate('history_data', group_by='device_id', metrics=['avg', 'count'])
"""

import logging
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DataAggregator:
    """数据聚合查询器"""

    SUPPORTED_AGGREGATIONS = {
        'count': 'COUNT(*)',
        'avg': 'AVG(value)',
        'sum': 'SUM(value)',
        'min': 'MIN(value)',
        'max': 'MAX(value)',
        'stddev': 'ROUND(SQRT(AVG(value * value) - AVG(value) * AVG(value)), 4)',
        'median': None,  # 需要特殊处理
        'percentile_95': None,
    }

    def __init__(self, db_path: str):
        self.db_path = db_path

    def aggregate(
        self,
        table: str,
        group_by: str,
        metrics: List[str],
        filters: Optional[Dict[str, Any]] = None,
        time_range: Optional[Dict[str, str]] = None,
        order_by: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        执行聚合查询

        Args:
            table: 表名
            group_by: 分组字段
            metrics: 聚合指标列表（count/avg/sum/min/max/stddev）
            filters: 过滤条件
            time_range: 时间范围 {'start': '...', 'end': '...'}
            order_by: 排序字段（格式：'metric_name desc'）
            limit: 结果限制

        Returns:
            {
                'data': [...],
                'summary': {...},
                'query_info': {...},
            }
        """
        # 安全校验
        if not table.isalnum() and '_' not in table:
            raise ValueError(f"无效的表名: {table}")
        if not group_by.isalnum() and '_' not in group_by:
            raise ValueError(f"无效的分组字段: {group_by}")

        # 构建聚合表达式
        agg_expressions = []
        for metric in metrics:
            if metric not in self.SUPPORTED_AGGREGATIONS:
                raise ValueError(f"不支持的聚合函数: {metric}")
            expr = self.SUPPORTED_AGGREGATIONS[metric]
            if expr:
                agg_expressions.append(f"{expr} AS {metric}")

        if not agg_expressions:
            raise ValueError("至少需要一个聚合指标")

        # 构建SQL
        sql = f'SELECT "{group_by}", {", ".join(agg_expressions)} FROM "{table}"'
        params: List[Any] = []

        # 过滤条件
        where_clauses = []
        if filters:
            for key, value in filters.items():
                if not key.isalnum() and '_' not in key:
                    continue
                where_clauses.append(f'"{key}" = ?')
                params.append(value)

        # 时间范围
        if time_range:
            if time_range.get('start'):
                where_clauses.append('timestamp >= ?')
                params.append(time_range['start'])
            if time_range.get('end'):
                where_clauses.append('timestamp <= ?')
                params.append(time_range['end'])

        if where_clauses:
            sql += ' WHERE ' + ' AND '.join(where_clauses)

        # GROUP BY
        sql += f' GROUP BY "{group_by}"'

        # 排序
        if order_by:
            parts = order_by.split()
            if len(parts) == 2 and parts[0] in metrics:
                direction = 'DESC' if parts[1].upper() == 'DESC' else 'ASC'
                sql += f' ORDER BY {parts[0]} {direction}'
        else:
            sql += f' ORDER BY COUNT(*) DESC'

        # 限制
        sql += f' LIMIT {min(limit, 1000)}'

        # 执行查询
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

            data = [dict(row) for row in rows]

            # 计算汇总
            summary = self._calculate_summary(data, metrics)

            return {
                'data': data,
                'summary': summary,
                'query_info': {
                    'table': table,
                    'group_by': group_by,
                    'metrics': metrics,
                    'row_count': len(data),
                },
            }
        finally:
            conn.close()

    def _calculate_summary(self, data: List[Dict], metrics: List[str]) -> Dict[str, Any]:
        """计算汇总统计"""
        summary = {'total_groups': len(data)}

        for metric in metrics:
            values = [row.get(metric) for row in data if row.get(metric) is not None]
            if values:
                summary[f'{metric}_total'] = sum(values)
                summary[f'{metric}_avg'] = sum(values) / len(values)
                summary[f'{metric}_min'] = min(values)
                summary[f'{metric}_max'] = max(values)

        return summary

    def time_series_aggregate(
        self,
        table: str,
        time_field: str = 'timestamp',
        interval: str = 'hour',
        metrics: List[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        time_range: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        时间序列聚合

        Args:
            table: 表名
            time_field: 时间字段
            interval: 时间间隔（minute/hour/day/week/month）
            metrics: 聚合指标
            filters: 过滤条件
            time_range: 时间范围
        """
        if metrics is None:
            metrics = ['count', 'avg']

        # SQLite时间格式化
        time_formats = {
            'minute': "strftime('%Y-%m-%d %H:%M', timestamp)",
            'hour': "strftime('%Y-%m-%d %H:00', timestamp)",
            'day': "strftime('%Y-%m-%d', timestamp)",
            'week': "strftime('%Y-W%W', timestamp)",
            'month': "strftime('%Y-%m', timestamp)",
        }

        if interval not in time_formats:
            raise ValueError(f"不支持的时间间隔: {interval}")

        time_expr = time_formats[interval]

        # 构建聚合表达式
        agg_expressions = []
        for metric in metrics:
            expr = self.SUPPORTED_AGGREGATIONS.get(metric)
            if expr:
                agg_expressions.append(f"{expr} AS {metric}")

        sql = f'SELECT {time_expr} AS time_bucket, {", ".join(agg_expressions)} FROM "{table}"'
        params: List[Any] = []

        # 过滤条件
        where_clauses = []
        if filters:
            for key, value in filters.items():
                where_clauses.append(f'"{key}" = ?')
                params.append(value)

        if time_range:
            if time_range.get('start'):
                where_clauses.append('timestamp >= ?')
                params.append(time_range['start'])
            if time_range.get('end'):
                where_clauses.append('timestamp <= ?')
                params.append(time_range['end'])

        if where_clauses:
            sql += ' WHERE ' + ' AND '.join(where_clauses)

        sql += f' GROUP BY time_bucket ORDER BY time_bucket'

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

            return {
                'data': [dict(row) for row in rows],
                'interval': interval,
                'query_info': {
                    'table': table,
                    'metrics': metrics,
                    'row_count': len(rows),
                },
            }
        finally:
            conn.close()


def create_aggregation_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """创建聚合查询响应"""
    return {
        'success': True,
        'data': result['data'],
        'summary': result.get('summary', {}),
        'query_info': result.get('query_info', {}),
    }
