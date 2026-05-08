"""
TDengine客户端封装

提供TDengine时序数据库的高级接口。

支持两种连接方式：
1. REST API（推荐）：通过HTTP连接，无需安装客户端库
2. 原生连接：通过taos连接，需要安装TDengine客户端库

使用方式：
    # REST API方式
    client = TDengineClient("localhost", 6041, use_rest=True)

    # 原生连接方式
    client = TDengineClient("localhost", 6030, use_rest=False)
"""

import logging
from datetime import datetime, timedelta
from typing import Any

import requests

from .data_models import (
    TelemetryRecord, AlarmRecord, OEERecord, EnergyRecord, PredictiveRecord,
    STABLE_DEFINITIONS,
    get_telemetry_table_name, get_alarm_table_name,
    get_oee_table_name, get_energy_table_name, get_predictive_table_name,
    get_create_table_sql
)


class TDengineClient:
    """
    TDengine客户端

    提供时序数据库的读写接口。
    """

    def __init__(self, host: str = "localhost", port: int = 6041,
                 user: str = "root", password: str = "taosdata",
                 database: str = "scada", use_rest: bool = True):
        """
        初始化TDengine客户端

        Args:
            host: TDengine主机地址
            port: 端口（REST API默认6041，原生连接默认6030）
            user: 用户名
            password: 密码
            database: 数据库名
            use_rest: 是否使用REST API
        """
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.use_rest = use_rest

        self.logger = logging.getLogger("TDengineClient")

        # REST API基础URL
        self.rest_url = f"http://{host}:{port}/rest/sql"

        # 原生连接（如果使用）
        self._connection = None

        # 统计信息
        self.stats: dict[str, Any] = {
            'writes': 0,
            'queries': 0,
            'errors': 0,
            'last_error': None
        }

        # 已创建的表缓存
        self._created_tables: set[str] = set()

    def connect(self) -> bool:
        """
        连接TDengine

        Returns:
            bool: 是否连接成功
        """
        try:
            if self.use_rest:
                return self._connect_rest()
            else:
                return self._connect_native()
        except Exception as e:
            self.logger.error(f"连接失败: {e}")
            return False

    def _connect_rest(self) -> bool:
        """通过REST API连接"""
        try:
            # 测试连接
            response = self._execute_sql("SELECT server_version()")
            if response:
                self.logger.info(f"TDengine REST API连接成功: {self.host}:{self.port}")
                # 创建数据库
                self._create_database()
                return True
            return False
        except Exception as e:
            self.logger.error(f"REST API连接失败: {e}")
            return False

    def _connect_native(self) -> bool:
        """通过原生连接"""
        try:
            import taos
            self._connection = taos.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database
            )
            self.logger.info(f"TDengine原生连接成功: {self.host}:{self.port}")
            return True
        except ImportError:
            self.logger.error("未安装taos库，请使用REST API或安装：pip install taos")
            return False
        except Exception as e:
            self.logger.error(f"原生连接失败: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        if self._connection:
            try:
                self._connection.close()
            except:
                pass
            self._connection = None

    def _create_database(self):
        """创建数据库"""
        sql = f"CREATE DATABASE IF NOT EXISTS {self.database} KEEP 365 DAYS 10 BLOCKS 6"
        self._execute_sql(sql)
        sql = f"USE {self.database}"
        self._execute_sql(sql)
        self.logger.info(f"数据库 {self.database} 已就绪")

    def _execute_sql(self, sql: str) -> dict[str, Any] | None:
        """
        执行SQL语句

        Args:
            sql: SQL语句

        Returns:
            dict[str, Any]: 查询结果
            None: 执行失败
        """
        if self.use_rest:
            return self._execute_rest(sql)
        else:
            return self._execute_native(sql)

    def _execute_rest(self, sql: str) -> dict[str, Any] | None:
        """通过REST API执行SQL"""
        try:
            auth = (self.user, self.password)
            headers = {'Content-Type': 'text/plain'}

            response = requests.post(
                self.rest_url,
                data=sql.encode('utf-8'),
                auth=auth,
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    return result
                else:
                    self.logger.error(f"SQL执行失败: {result.get('desc')}")
                    self.stats['errors'] += 1
                    self.stats['last_error'] = result.get('desc')
                    return None
            else:
                self.logger.error(f"HTTP错误: {response.status_code}")
                self.stats['errors'] += 1
                return None

        except Exception as e:
            self.logger.error(f"REST API异常: {e}")
            self.stats['errors'] += 1
            self.stats['last_error'] = str(e)
            return None

    def _execute_native(self, sql: str) -> dict[str, Any] | None:
        """通过原生连接执行SQL"""
        if not self._connection:
            self.logger.error("未连接")
            return None

        try:
            cursor = self._connection.cursor()
            cursor.execute(sql)

            if cursor.description:
                # 查询语句
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                return {
                    'code': 0,
                    'column_meta': [[col] for col in columns],
                    'data': rows
                }
            else:
                # 写入语句
                return {'code': 0, 'rows_affected': cursor.rowcount}

        except Exception as e:
            self.logger.error(f"原生执行异常: {e}")
            self.stats['errors'] += 1
            self.stats['last_error'] = str(e)
            return None

    def init_tables(self):
        """
        初始化所有超级表

        应在系统启动时调用一次。
        """
        self.logger.info("初始化TDengine超级表...")

        for table_name, create_sql in STABLE_DEFINITIONS.items():
            result = self._execute_sql(create_sql)
            if result:
                self.logger.info(f"超级表 {table_name} 已就绪")
            else:
                self.logger.error(f"创建超级表 {table_name} 失败")

    def _ensure_table(self, table_name: str, stable_name: str, tags: dict[str, str]):
        """确保子表存在"""
        if table_name in self._created_tables:
            return

        sql = get_create_table_sql(table_name, stable_name, tags)
        result = self._execute_sql(sql)
        if result:
            self._created_tables.add(table_name)

    # ==================== 数据写入 ====================

    def write_telemetry(self, record: TelemetryRecord):
        """
        写入遥测数据

        Args:
            record: 遥测数据记录
        """
        table_name = get_telemetry_table_name(record.device_id, record.register_name)

        # 确保子表存在
        self._ensure_table(table_name, 'device_telemetry', {
            'device_id': record.device_id,
            'register_name': record.register_name,
            'unit': record.unit,
            'protocol': record.protocol,
            'gateway_id': record.gateway_id
        })

        # 构造INSERT语句
        ts = record.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        sql = f"INSERT INTO {table_name} VALUES ('{ts}', {record.value}, {record.quality})"

        result = self._execute_sql(sql)
        if result:
            self.stats['writes'] += 1
        else:
            self.logger.error(f"写入遥测数据失败: {record.device_id}.{record.register_name}")

    def write_telemetry_batch(self, records: list[TelemetryRecord]):
        """
        批量写入遥测数据

        Args:
            records: 遥测数据记录列表
        """
        if not records:
            return

        # 按表分组
        table_groups: dict[str, list[TelemetryRecord]] = {}
        for record in records:
            table_name = get_telemetry_table_name(record.device_id, record.register_name)
            if table_name not in table_groups:
                table_groups[table_name] = []
            table_groups[table_name].append(record)

        # 批量写入每个表
        for table_name, table_records in table_groups.items():
            # 确保表存在
            first = table_records[0]
            self._ensure_table(table_name, 'device_telemetry', {
                'device_id': first.device_id,
                'register_name': first.register_name,
                'unit': first.unit,
                'protocol': first.protocol,
                'gateway_id': first.gateway_id
            })

            # 构造批量INSERT
            values = []
            for record in table_records:
                ts = record.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                values.append(f"('{ts}', {record.value}, {record.quality})")

            sql = f"INSERT INTO {table_name} VALUES {', '.join(values)}"
            result = self._execute_sql(sql)

            if result:
                self.stats['writes'] += len(table_records)
            else:
                self.logger.error(f"批量写入失败: {table_name}")

    def write_alarm(self, record: AlarmRecord):
        """写入报警记录"""
        table_name = get_alarm_table_name(record.device_id)

        self._ensure_table(table_name, 'alarm_records', {
            'device_id': record.device_id,
            'alarm_id': record.alarm_id
        })

        sql = f"INSERT INTO {table_name} VALUES {record.to_sql_values()}"
        result = self._execute_sql(sql)
        if result:
            self.stats['writes'] += 1

    def write_oee(self, record: OEERecord):
        """写入OEE记录"""
        table_name = get_oee_table_name(record.device_id)

        self._ensure_table(table_name, 'oee_records', {
            'device_id': record.device_id
        })

        sql = f"INSERT INTO {table_name} VALUES {record.to_sql_values()}"
        result = self._execute_sql(sql)
        if result:
            self.stats['writes'] += 1

    def write_energy(self, record: EnergyRecord):
        """写入能源记录"""
        table_name = get_energy_table_name(record.device_id)

        self._ensure_table(table_name, 'energy_records', {
            'device_id': record.device_id
        })

        sql = f"INSERT INTO {table_name} VALUES {record.to_sql_values()}"
        result = self._execute_sql(sql)
        if result:
            self.stats['writes'] += 1

    def write_predictive(self, record: PredictiveRecord):
        """写入预测性维护记录"""
        table_name = get_predictive_table_name(record.device_id)

        self._ensure_table(table_name, 'predictive_records', {
            'device_id': record.device_id
        })

        sql = f"INSERT INTO {table_name} VALUES {record.to_sql_values()}"
        result = self._execute_sql(sql)
        if result:
            self.stats['writes'] += 1

    # ==================== 数据查询 ====================

    def query_telemetry(self, device_id: str, register_name: str,
                        start_time: datetime, end_time: datetime,
                        limit: int = 1000) -> list[dict[str, Any]]:
        """
        查询遥测数据

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            start_time: 开始时间
            end_time: 结束时间
            limit: 返回记录数限制

        Returns:
            list[dict[str, Any]]: 数据列表
        """
        table_name = get_telemetry_table_name(device_id, register_name)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            SELECT ts, value, quality 
            FROM {table_name} 
            WHERE ts >= '{start_ts}' AND ts <= '{end_ts}'
            ORDER BY ts DESC
            LIMIT {limit}
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self.stats['queries'] += 1
            return self._format_query_result(result)
        return []

    def query_telemetry_latest(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """查询最新的遥测数据"""
        table_name = get_telemetry_table_name(device_id, register_name)

        sql = f"SELECT LAST(ts, value, quality) FROM {table_name}"

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self.stats['queries'] += 1
            data = result['data'][0]
            return {
                'timestamp': data[0],
                'value': data[1],
                'quality': data[2]
            }
        return None

    def query_telemetry_agg(self, device_id: str, register_name: str,
                            start_time: datetime, end_time: datetime,
                            interval: str = "1h") -> list[dict[str, Any]]:
        """
        查询遥测数据聚合结果

        Args:
            interval: 聚合间隔，如 "1m", "5m", "1h", "1d"
        """
        table_name = get_telemetry_table_name(device_id, register_name)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            SELECT _wstart as ts, AVG(value) as avg_val, 
                   MAX(value) as max_val, MIN(value) as min_val,
                   COUNT(*) as count
            FROM {table_name}
            WHERE ts >= '{start_ts}' AND ts <= '{end_ts}'
            INTERVAL({interval})
            ORDER BY ts
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self.stats['queries'] += 1
            return self._format_query_result(result, 
                columns=['timestamp', 'avg_value', 'max_value', 'min_value', 'count'])
        return []

    def query_alarms(self, device_id: str, start_time: datetime, end_time: datetime,
                     level: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """查询报警记录"""
        table_name = get_alarm_table_name(device_id)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        where_clauses = [f"ts >= '{start_ts}'", f"ts <= '{end_ts}'"]
        if level:
            where_clauses.append(f"level = '{level}'")

        sql = f"""
            SELECT ts, level, alarm_type, message, value, threshold, acknowledged
            FROM {table_name}
            WHERE {' AND '.join(where_clauses)}
            ORDER BY ts DESC
            LIMIT {limit}
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self.stats['queries'] += 1
            return self._format_query_result(result,
                columns=['timestamp', 'level', 'alarm_type', 'message', 
                        'value', 'threshold', 'acknowledged'])
        return []

    def query_oee(self, device_id: str, start_time: datetime, end_time: datetime) -> list[dict[str, Any]]:
        """查询OEE记录"""
        table_name = get_oee_table_name(device_id)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            SELECT ts, availability, performance, quality_rate, oee,
                   total_count, good_count, run_time, downtime
            FROM {table_name}
            WHERE ts >= '{start_ts}' AND ts <= '{end_ts}'
            ORDER BY ts DESC
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self.stats['queries'] += 1
            return self._format_query_result(result,
                columns=['timestamp', 'availability', 'performance', 'quality_rate', 'oee',
                        'total_count', 'good_count', 'run_time', 'downtime'])
        return []

    def query_energy(self, device_id: str, start_time: datetime, end_time: datetime,
                     interval: str = "1h") -> list[dict[str, Any]]:
        """查询能源数据聚合"""
        table_name = get_energy_table_name(device_id)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            SELECT _wstart as ts, AVG(power) as avg_power,
                   MAX(power) as max_power, SUM(energy) as total_energy
            FROM {table_name}
            WHERE ts >= '{start_ts}' AND ts <= '{end_ts}'
            INTERVAL({interval})
            ORDER BY ts
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self.stats['queries'] += 1
            return self._format_query_result(result,
                columns=['timestamp', 'avg_power', 'max_power', 'total_energy'])
        return []

    def _format_query_result(self, result: dict[str, Any], columns: list[str] | None = None) -> list[dict[str, Any]]:
        """格式化查询结果"""
        if not result or not result.get('data'):
            return []

        # 获取列名
        if columns is None:
            column_meta = result.get('column_meta', [])
            columns = [meta[0] for meta in column_meta]

        # 转换为字典列表
        formatted: list[dict[str, Any]] = []
        for row in result['data']:
            record: dict[str, Any] = {}
            for i, col in enumerate(columns):
                if i < len(row):
                    record[col] = row[i]
            formatted.append(record)

        return formatted

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return self.stats.copy()


# 测试代码
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 创建客户端
    client = TDengineClient("localhost", 6041)

    # 连接
    if not client.connect():
        print("连接失败，请确保TDengine已启动")
        sys.exit(1)

    # 初始化表
    client.init_tables()

    # 测试写入
    print("\n测试写入遥测数据...")
    record = TelemetryRecord(
        device_id="CNC_001",
        register_name="temperature",
        timestamp=datetime.now(),
        value=25.5,
        quality=192,
        unit="°C",
        protocol="ModbusTCP",
        gateway_id="gateway_01"
    )
    client.write_telemetry(record)

    # 测试查询
    print("\n测试查询遥测数据...")
    end_time = datetime.now()
    start_time = end_time - timedelta(hours=1)
    data = client.query_telemetry("CNC_001", "temperature", start_time, end_time)
    print(f"查询到 {len(data)} 条记录")

    # 打印统计
    print(f"\n统计信息: {client.get_stats()}")
