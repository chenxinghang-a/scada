"""
TDengine查询构建器

提供SQL查询的构建工具，支持：
- 时间范围查询
- 聚合查询（AVG, MAX, MIN, SUM, COUNT）
- 降采样（INTERVAL）
- 多表联合查询
- 条件过滤

使用方式：
    builder = QueryBuilder("device_telemetry")
    sql = (builder
           .select("ts", "value", "quality")
           .where_device("CNC_001")
           .where_time(start_time, end_time)
           .interval("1h")
           .order_by("ts", "DESC")
           .limit(100)
           .build())
"""

from datetime import datetime


class QueryBuilder:
    """
    SQL查询构建器

    支持链式调用，构建复杂的TDengine查询。
    """

    def __init__(self, table: str):
        """
        初始化查询构建器

        Args:
            table: 表名（可以是超级表或子表）
        """
        self._table = table
        self._select_columns: list[str] = []
        self._where_clauses: list[str] = []
        self._group_by: list[str] = []
        self._order_by: str | None = None
        self._limit: int | None = None
        self._offset: int | None = None
        self._interval: str | None = None
        self._fill: str | None = None
        self._aggregate_funcs: dict[str, str] = {}

    def select(self, *columns: str) -> 'QueryBuilder':
        """
        选择列

        Args:
            columns: 列名列表
        """
        self._select_columns.extend(columns)
        return self

    def select_agg(self, func: str, column: str, alias: str | None = None) -> 'QueryBuilder':
        """
        选择聚合列

        Args:
            func: 聚合函数 (AVG, MAX, MIN, SUM, COUNT, FIRST, LAST, TOP, BOTTOM)
            column: 列名
            alias: 别名
        """
        alias = alias or f"{func.lower()}_{column}"
        self._aggregate_funcs[alias] = f"{func}({column})"
        return self

    def where(self, condition: str) -> 'QueryBuilder':
        """
        添加WHERE条件

        Args:
            condition: SQL条件表达式
        """
        self._where_clauses.append(condition)
        return self

    def where_device(self, device_id: str) -> 'QueryBuilder':
        """添加设备ID条件"""
        self._where_clauses.append(f"device_id = '{device_id}'")
        return self

    def where_time(self, start: datetime, end: datetime) -> 'QueryBuilder':
        """
        添加时间范围条件

        Args:
            start: 开始时间
            end: 结束时间
        """
        start_ts = start.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end.strftime('%Y-%m-%d %H:%M:%S')
        self._where_clauses.append(f"ts >= '{start_ts}'")
        self._where_clauses.append(f"ts <= '{end_ts}'")
        return self

    def where_level(self, level: str) -> 'QueryBuilder':
        """添加报警级别条件"""
        self._where_clauses.append(f"level = '{level}'")
        return self

    def where_value_gt(self, value: float) -> 'QueryBuilder':
        """添加值大于条件"""
        self._where_clauses.append(f"value > {value}")
        return self

    def where_value_lt(self, value: float) -> 'QueryBuilder':
        """添加值小于条件"""
        self._where_clauses.append(f"value < {value}")
        return self

    def where_quality(self, quality: int = 192) -> 'QueryBuilder':
        """添加数据质量条件"""
        self._where_clauses.append(f"quality = {quality}")
        return self

    def group_by(self, *columns: str) -> 'QueryBuilder':
        """
        添加GROUP BY

        Args:
            columns: 分组列
        """
        self._group_by.extend(columns)
        return self

    def order_by(self, column: str, direction: str = "ASC") -> 'QueryBuilder':
        """
        添加ORDER BY

        Args:
            column: 排序列
            direction: 排序方向 (ASC, DESC)
        """
        self._order_by = f"{column} {direction}"
        return self

    def limit(self, count: int) -> 'QueryBuilder':
        """设置返回记录数限制"""
        self._limit = count
        return self

    def offset(self, count: int) -> 'QueryBuilder':
        """设置偏移量"""
        self._offset = count
        return self

    def interval(self, interval: str, fill: str | None = None) -> 'QueryBuilder':
        """
        设置时间间隔（用于降采样）

        Args:
            interval: 时间间隔，如 "1m", "5m", "1h", "1d"
            fill: 填充方式 (NULL, PREV, LINEAR, NONE)
        """
        self._interval = interval
        if fill:
            self._fill = fill
        return self

    def build(self) -> str:
        """
        构建SQL语句

        Returns:
            str: SQL语句
        """
        # SELECT子句
        select_parts = []

        # 添加普通列
        if self._select_columns:
            select_parts.extend(self._select_columns)

        # 添加聚合列
        for alias, func in self._aggregate_funcs.items():
            select_parts.append(f"{func} as {alias}")

        # 如果没有选择任何列，默认选择所有
        if not select_parts:
            select_parts.append("*")

        select_clause = ", ".join(select_parts)

        # FROM子句
        from_clause = self._table

        # WHERE子句
        where_clause = ""
        if self._where_clauses:
            where_clause = f"WHERE {' AND '.join(self._where_clauses)}"

        # GROUP BY子句
        group_clause = ""
        if self._group_by:
            group_clause = f"GROUP BY {', '.join(self._group_by)}"

        # INTERVAL子句（TDengine特有）
        interval_clause = ""
        if self._interval:
            interval_clause = f"INTERVAL({self._interval})"
            if self._fill:
                interval_clause += f" FILL({self._fill})"

        # ORDER BY子句
        order_clause = ""
        if self._order_by:
            order_clause = f"ORDER BY {self._order_by}"

        # LIMIT子句
        limit_clause = ""
        if self._limit is not None:
            limit_clause = f"LIMIT {self._limit}"
            if self._offset is not None:
                limit_clause += f" OFFSET {self._offset}"

        # 组装SQL
        parts = [
            f"SELECT {select_clause}",
            f"FROM {from_clause}",
            where_clause,
            group_clause,
            interval_clause,
            order_clause,
            limit_clause
        ]

        # 过滤空部分
        sql = " ".join(part for part in parts if part)

        return sql

    def build_count(self) -> str:
        """构建COUNT查询"""
        return f"SELECT COUNT(*) FROM {self._table} {'WHERE ' + ' AND '.join(self._where_clauses) if self._where_clauses else ''}"

    def build_latest(self, *columns: str) -> str:
        """构建查询最新记录的SQL"""
        if not columns:
            columns = ("ts", "value", "quality")

        cols = ", ".join(columns)
        return f"SELECT LAST({cols}) FROM {self._table}"

    def build_first(self, *columns: str) -> str:
        """构建查询最早记录的SQL"""
        if not columns:
            columns = ("ts", "value", "quality")

        cols = ", ".join(columns)
        return f"SELECT FIRST({cols}) FROM {self._table}"


class TelemetryQueryBuilder(QueryBuilder):
    """遥测数据查询构建器"""

    def __init__(self, device_id: str, register_name: str):
        from .data_models import get_telemetry_table_name
        table_name = get_telemetry_table_name(device_id, register_name)
        super().__init__(table_name)
        self._device_id = device_id
        self._register_name = register_name

    def with_quality(self, quality: int = 192) -> 'TelemetryQueryBuilder':
        """只查询良好质量的数据"""
        self.where_quality(quality)
        return self


class AlarmQueryBuilder(QueryBuilder):
    """报警查询构建器"""

    def __init__(self, device_id: str):
        from .data_models import get_alarm_table_name
        table_name = get_alarm_table_name(device_id)
        super().__init__(table_name)
        self._device_id = device_id

    def critical_only(self) -> 'AlarmQueryBuilder':
        """只查询严重报警"""
        self.where_level("critical")
        return self

    def warning_only(self) -> 'AlarmQueryBuilder':
        """只查询警告"""
        self.where_level("warning")
        return self

    def unacknowledged(self) -> 'AlarmQueryBuilder':
        """只查询未确认的报警"""
        self.where("acknowledged = 0")
        return self


class OEEQueryBuilder(QueryBuilder):
    """OEE查询构建器"""

    def __init__(self, device_id: str):
        from .data_models import get_oee_table_name
        table_name = get_oee_table_name(device_id)
        super().__init__(table_name)
        self._device_id = device_id


class EnergyQueryBuilder(QueryBuilder):
    """能源查询构建器"""

    def __init__(self, device_id: str):
        from .data_models import get_energy_table_name
        table_name = get_energy_table_name(device_id)
        super().__init__(table_name)
        self._device_id = device_id


# 测试代码
if __name__ == "__main__":
    # 测试查询构建器
    from datetime import datetime, timedelta

    end_time = datetime.now()
    start_time = end_time - timedelta(hours=24)

    # 遥测数据查询
    builder = TelemetryQueryBuilder("CNC_001", "temperature")
    sql = (builder
           .select("ts", "value", "quality")
           .where_time(start_time, end_time)
           .with_quality()
           .interval("1h")
           .order_by("ts", "DESC")
           .limit(100)
           .build())

    print("遥测数据查询:")
    print(sql)
    print()

    # 报警查询
    alarm_builder = AlarmQueryBuilder("CNC_001")
    sql = (alarm_builder
           .select("ts", "level", "message", "value")
           .where_time(start_time, end_time)
           .critical_only()
           .order_by("ts", "DESC")
           .limit(50)
           .build())

    print("报警查询:")
    print(sql)
    print()

    # OEE聚合查询
    oee_builder = OEEQueryBuilder("CNC_001")
    sql = (oee_builder
           .select_agg("AVG", "oee", "avg_oee")
           .select_agg("AVG", "availability", "avg_availability")
           .where_time(start_time, end_time)
           .interval("1d")
           .build())

    print("OEE聚合查询:")
    print(sql)
