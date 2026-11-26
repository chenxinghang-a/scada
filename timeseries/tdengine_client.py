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

import contextlib
import logging
import re
import threading
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
        # BUG-086：`/rest/sql` 是**无状态**接口 —— 上一条 `USE db` 在下一条
        # HTTP 请求里就失效了，而建表/写入全都不带库名，等于在"无库上下文"
        # 里执行。必须把库名放进 URL 路径，每条请求自带库上下文。
        self.rest_url = (
            f"http://{host}:{port}/rest/sql/{self._sanitize_identifier(database)}")

        # 原生连接（如果使用）
        self._connection = None

        # 并发锁：保护原生连接、建表缓存与统计字典
        # 支持 TDengineClient.__new__(TDengineClient) 单测骨架 via _lock_ctx。
        self._conn_lock = threading.Lock()
        self._tables_lock = threading.Lock()
        self._stats_lock = threading.Lock()

        # 统计信息
        self.stats: dict[str, Any] = {
            'writes': 0,
            'queries': 0,
            'errors': 0,
            'quarantined': 0,
            'last_error': None
        }

        # 已创建的表缓存
        self._created_tables: set[str] = set()

    def _lock_ctx(self, attr: str):
        """获取指定锁的上下文管理器；兼容未走 __init__ 的单测桩对象。"""
        lock = getattr(self, attr, None)
        return lock if lock is not None else contextlib.nullcontext()

    def _bump_stat(self, key: str, delta: int = 1, last_error: str | None = None) -> None:
        with self._lock_ctx('_stats_lock'):
            self.stats[key] = self.stats.get(key, 0) + delta
            if last_error is not None:
                self.stats['last_error'] = last_error

    @staticmethod
    def _is_valid_id_str(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip()) and '\x00' not in value

    @staticmethod
    def _sanitize_identifier(name: str) -> str:
        """防SQL注入：只允许字母数字下划线"""
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', str(name)):
            raise ValueError(f"Invalid SQL identifier: {name}")
        return str(name)

    @staticmethod
    def _escape_value(val: str) -> str:
        """防SQL注入：转义字符串值"""
        return str(val).replace("\\", "\\\\").replace("'", "\\'").replace("\x00", "")

    @staticmethod
    def _validate_limit(limit: Any, max_limit: int = 10000) -> int:
        """校验 LIMIT：只接受 1..max_limit 的**真整数**，其余一律抛 ValueError。

        `LIMIT {limit}` 是直接拼进 SQL 的（无法参数化），所以 `limit` 必须是
        int —— 字符串会变成注入面（`1 UNION ALL SELECT ...`），
        超大值会让 TDengine 全表拉取并把进程拖死，两者都必须拒绝而不是截断。
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError(f'LIMIT 必须是整数: {limit!r}')
        if not (1 <= limit <= max_limit):
            raise ValueError(f'LIMIT 超出范围 1..{max_limit}: {limit!r}')
        return limit

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
            # 首次部署库还不存在：带库名的 /rest/sql/{db} 探活会失败，
            # 建库永远走不到。探活/建库都走不带库名的端点，且不临时篡改 self.rest_url
            # 以免并发写入线程误用无库 URL。
            base_url = f"http://{self.host}:{self.port}/rest/sql"
            if not self._execute_sql("SELECT server_version()", url=base_url):
                return False
            self._create_database(url=base_url)
            self.logger.info(f"TDengine REST API连接成功: {self.host}:{self.port}")
            return True
        except Exception as e:
            self.logger.error(f"REST API连接失败: {e}")
            return False

    def _connect_native(self) -> bool:
        """通过原生连接"""
        try:
            import taos
            conn = taos.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database
            )
            with self._lock_ctx('_conn_lock'):
                old = self._connection
                self._connection = conn
            if old is not None and old is not conn:
                try:
                    old.close()
                except Exception as close_err:
                    self.logger.debug(f"TDengine 旧连接关闭失败: {close_err}")
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
        with self._lock_ctx('_conn_lock'):
            conn = self._connection
            self._connection = None
        if conn:
            try:
                conn.close()
            except Exception as e:
                # 关闭失败不影响断开语义：连接对象随后被置空丢弃
                self.logger.debug(f"TDengine 连接关闭失败 {self.host}:{self.port}: {e}")

    def _create_database(self, url: str | None = None):
        """创建数据库"""
        db_name = self._sanitize_identifier(self.database)
        sql = f"CREATE DATABASE IF NOT EXISTS {db_name} KEEP 365 DAYS 10 BLOCKS 6"
        if url is None:
            self._execute_sql(sql)
        else:
            self._execute_sql(sql, url=url)
        # BUG-086：不再发 `USE db`（REST 无会话，发了也不会生效）。
        # 库上下文已由 rest_url 的 `/{database}` 路径携带。
        self.logger.info(f"数据库 {self.database} 已就绪")

    def _execute_sql(self, sql: str, url: str | None = None) -> dict[str, Any] | None:
        """
        执行SQL语句

        Args:
            sql: SQL语句
            url: 可选的 REST 端点覆盖（仅用于建库/探活，不改写 self.rest_url）

        Returns:
            dict[str, Any]: 查询结果
            None: 执行失败
        """
        if self.use_rest:
            if url is None:
                return self._execute_rest(sql)
            return self._execute_rest(sql, url=url)
        else:
            return self._execute_native(sql)

    def _execute_rest(self, sql: str, url: str | None = None) -> dict[str, Any] | None:
        """通过REST API执行SQL"""
        try:
            auth = (self.user, self.password)
            headers = {'Content-Type': 'text/plain'}
            target_url = url or self.rest_url

            response = requests.post(
                target_url,
                data=sql.encode('utf-8'),
                auth=auth,
                headers=headers,
                timeout=30,
                # round 364：**本地/内网时序库必须直连，不能走系统代理**。
                # `requests` 默认会读环境变量里的 `HTTP_PROXY` / `HTTPS_PROXY`，
                # 而开发机和不少现场服务器都配了代理 —— 于是连
                # `localhost:6041` 也去走代理，报
                # `ProxyError: Unable to connect to proxy`，
                # 日志还是 **ERROR** 级别，看起来像「TDengine 挂了」。
                # 时序库永远部署在本机或内网，这里显式关掉代理。
                proxies={'http': None, 'https': None},
            )

            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    return result
                else:
                    desc = result.get('desc')
                    self.logger.error(f"SQL执行失败: {desc}")
                    self._bump_stat('errors', 1, last_error=desc)
                    return None
            else:
                self.logger.error(f"HTTP错误: {response.status_code}")
                self._bump_stat('errors', 1)
                return None

        except Exception as e:
            self.logger.error(f"REST API异常: {e}")
            self._bump_stat('errors', 1, last_error=str(e))
            return None

    def _execute_native(self, sql: str) -> dict[str, Any] | None:
        """通过原生连接执行SQL"""
        err_msg: str | None = None
        with self._lock_ctx('_conn_lock'):
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
                err_msg = str(e)
                self.logger.error(f"原生执行异常: {e}")

        if err_msg is not None:
            self._bump_stat('errors', 1, last_error=err_msg)
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
        with self._lock_ctx('_tables_lock'):
            if table_name in self._created_tables:
                return

        # Sanitize identifiers before passing to SQL generation
        safe_table = self._sanitize_identifier(table_name)
        safe_stable = self._sanitize_identifier(stable_name)
        sql = get_create_table_sql(safe_table, safe_stable, tags)
        result = self._execute_sql(sql)
        if result:
            with self._lock_ctx('_tables_lock'):
                self._created_tables.add(table_name)

    def _quarantine_record(self, context: str, reason: Any) -> None:
        self.logger.error("非法时序数据已隔离丢弃（%s）: %s", context, reason)
        self._bump_stat('errors', 1, last_error=str(reason))
        self._bump_stat('quarantined', 1)

    def _write_single_record(self, table_name: str, stable_name: str,
                             tags: dict[str, Any], record: Any,
                             label: str) -> bool:
        """校验单条记录、建表并写入 TDengine；非法数据隔离并返回 False。"""
        if not self._is_valid_id_str(getattr(record, 'device_id', None)):
            self._quarantine_record(label, f"device_id 非法: {getattr(record, 'device_id', None)!r}")
            return False
        if stable_name == 'alarm_records' and not self._is_valid_id_str(getattr(record, 'alarm_id', None)):
            self._quarantine_record(label, f"alarm_id 非法: {getattr(record, 'alarm_id', None)!r}")
            return False
        try:
            safe_table = self._sanitize_identifier(table_name)
            safe_stable = self._sanitize_identifier(stable_name)
            # 预先校验建表 SQL 与 values，避免坏数据污染建表缓存或拼出非法 SQL
            get_create_table_sql(safe_table, safe_stable, tags)
            values = record.to_sql_values()
        except (ValueError, TypeError, OverflowError, AttributeError) as e:
            self._quarantine_record(table_name, e)
            return False

        self._ensure_table(table_name, stable_name, tags)
        sql = f"INSERT INTO {safe_table} VALUES {values}"
        result = self._execute_sql(sql)
        if result:
            self._bump_stat('writes', 1)
            return True
        # round 298：**失败必须能被调用方判定**。
        # BUG-086：`_execute_sql` 内部已经 `stats['errors'] += 1`，不重复累加。
        self.logger.error("写入失败（%s）: %s", table_name, sql[:120])
        return False

    # ==================== 数据写入 ====================

    def write_telemetry(self, record: TelemetryRecord) -> bool:
        """
        写入遥测数据

        Args:
            record: 遥测数据记录
        """
        if not self._is_valid_id_str(getattr(record, 'device_id', None)) or \
                not self._is_valid_id_str(getattr(record, 'register_name', None)):
            self._quarantine_record(
                'device_telemetry',
                f"非法标识符 device_id={getattr(record, 'device_id', None)!r}, "
                f"register_name={getattr(record, 'register_name', None)!r}",
            )
            return False

        table_name = get_telemetry_table_name(record.device_id, record.register_name)
        tags = {
            'device_id': record.device_id,
            'register_name': record.register_name,
            'unit': record.unit,
            'protocol': record.protocol,
            'gateway_id': record.gateway_id
        }
        return self._write_single_record(
            table_name, 'device_telemetry', tags, record, table_name)

    def write_telemetry_batch(self, records: list[TelemetryRecord]) -> bool:
        """
        批量写入遥测数据

        Args:
            records: 遥测数据记录列表

        Returns:
            bool: 数据库写入通道正常返回 True；坏记录（毒丸）单条隔离丢弃，
                  不导致整批无限回填或触发熔断；仅在数据库执行失败时返回 False。
        """
        if not records:
            return True

        # 先单条校验并按表分组，毒丸记录只隔离丢弃不阻塞整批
        table_groups: dict[str, tuple[dict[str, Any], list[str]]] = {}
        for record in records:
            dev_id = getattr(record, 'device_id', None)
            reg_name = getattr(record, 'register_name', None)
            if not self._is_valid_id_str(dev_id) or not self._is_valid_id_str(reg_name):
                self._quarantine_record(
                    'device_telemetry',
                    f"非法标识符 device_id={dev_id!r}, register_name={reg_name!r}",
                )
                continue
            try:
                table_name = get_telemetry_table_name(dev_id, reg_name)
                safe_table = self._sanitize_identifier(table_name)
                tags = {
                    'device_id': dev_id,
                    'register_name': reg_name,
                    'unit': getattr(record, 'unit', ''),
                    'protocol': getattr(record, 'protocol', ''),
                    'gateway_id': getattr(record, 'gateway_id', ''),
                }
                get_create_table_sql(safe_table, 'device_telemetry', tags)
                sql_val = record.to_sql_values()
            except (ValueError, TypeError, OverflowError, AttributeError) as e:
                self._quarantine_record(f"{dev_id}.{reg_name}", e)
                continue

            if table_name not in table_groups:
                table_groups[table_name] = (tags, [])
            table_groups[table_name][1].append(sql_val)

        # 批量写入每个有效表
        all_ok = True
        for table_name, (tags, values) in table_groups.items():
            # 确保表存在
            self._ensure_table(table_name, 'device_telemetry', tags)

            # 构造批量INSERT
            safe_table = self._sanitize_identifier(table_name)
            sql = f"INSERT INTO {safe_table} VALUES {', '.join(values)}"
            result = self._execute_sql(sql)

            if result:
                self._bump_stat('writes', len(values))
            else:
                # 任一表失败 → 整批按失败上报（调用方据此回填重试，宁可重复不可丢）
                all_ok = False
                self.logger.error(f"批量写入失败: {table_name}")

        return all_ok

    def write_alarm(self, record: AlarmRecord) -> bool:
        """写入报警记录"""
        dev_id = getattr(record, 'device_id', None)
        if not self._is_valid_id_str(dev_id):
            self._quarantine_record('alarm_records', f"device_id 非法: {dev_id!r}")
            return False
        table_name = get_alarm_table_name(dev_id)
        return self._write_single_record(
            table_name,
            'alarm_records',
            {'device_id': dev_id, 'alarm_id': getattr(record, 'alarm_id', None)},
            record,
            table_name,
        )

    def write_oee(self, record: OEERecord) -> bool:
        """写入OEE记录"""
        dev_id = getattr(record, 'device_id', None)
        if not self._is_valid_id_str(dev_id):
            self._quarantine_record('oee_records', f"device_id 非法: {dev_id!r}")
            return False
        table_name = get_oee_table_name(dev_id)
        return self._write_single_record(
            table_name, 'oee_records', {'device_id': dev_id}, record, table_name)

    def write_energy(self, record: EnergyRecord) -> bool:
        """写入能源记录"""
        dev_id = getattr(record, 'device_id', None)
        if not self._is_valid_id_str(dev_id):
            self._quarantine_record('energy_records', f"device_id 非法: {dev_id!r}")
            return False
        table_name = get_energy_table_name(dev_id)
        return self._write_single_record(
            table_name, 'energy_records', {'device_id': dev_id}, record, table_name)

    def write_predictive(self, record: PredictiveRecord) -> bool:
        """写入预测性维护记录"""
        dev_id = getattr(record, 'device_id', None)
        if not self._is_valid_id_str(dev_id):
            self._quarantine_record('predictive_records', f"device_id 非法: {dev_id!r}")
            return False
        table_name = get_predictive_table_name(dev_id)
        return self._write_single_record(
            table_name, 'predictive_records', {'device_id': dev_id}, record, table_name)

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
        safe_table = self._sanitize_identifier(table_name)
        limit = self._validate_limit(limit)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            SELECT ts, value, quality
            FROM {safe_table}
            WHERE ts >= '{start_ts}' AND ts <= '{end_ts}'
            ORDER BY ts DESC
            LIMIT {limit}
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self._bump_stat('queries', 1)
            # BUG-086：TDengine 返回的列名是 `ts`，而上层（tsdb_adapter 等）
            # 一律读 `record['timestamp']` —— 不显式映射就全是 None。
            # 聚合/能源查询早就这么传了，这里漏了。
            return self._format_query_result(
                result, columns=['timestamp', 'value', 'quality'])
        return []

    def query_telemetry_latest(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """查询最新的遥测数据"""
        table_name = get_telemetry_table_name(device_id, register_name)
        safe_table = self._sanitize_identifier(table_name)

        sql = f"SELECT ts, value, quality FROM {safe_table} ORDER BY ts DESC LIMIT 1"

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self._bump_stat('queries', 1)
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
        # BUG-086：`interval` 是**直接拼进 SQL** 的（`INTERVAL({interval})`），
        # 不校验就是注入面 + 语法错误面。与 query_builder 的取值口径对齐。
        if not re.match(r'^\d{1,6}[smhdwn]$', str(interval or ''), re.IGNORECASE):
            raise ValueError(f'非法 interval: {interval!r}')
        table_name = get_telemetry_table_name(device_id, register_name)
        safe_table = self._sanitize_identifier(table_name)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            SELECT _wstart as ts, AVG(value) as avg_val,
                   MAX(value) as max_val, MIN(value) as min_val,
                   COUNT(*) as count
            FROM {safe_table}
            WHERE ts >= '{start_ts}' AND ts <= '{end_ts}'
            INTERVAL({interval})
            ORDER BY ts
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self._bump_stat('queries', 1)
            return self._format_query_result(result, 
                columns=['timestamp', 'avg_value', 'max_value', 'min_value', 'count'])
        return []

    def query_alarms(self, device_id: str, start_time: datetime, end_time: datetime,
                     level: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """查询报警记录"""
        table_name = get_alarm_table_name(device_id)
        safe_table = self._sanitize_identifier(table_name)
        limit = self._validate_limit(limit)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        where_clauses = [f"ts >= '{start_ts}'", f"ts <= '{end_ts}'"]
        if level:
            where_clauses.append(f"level = '{self._escape_value(level)}'")

        sql = f"""
            SELECT ts, level, alarm_type, message, value, threshold, acknowledged
            FROM {safe_table}
            WHERE {' AND '.join(where_clauses)}
            ORDER BY ts DESC
            LIMIT {limit}
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self._bump_stat('queries', 1)
            return self._format_query_result(result,
                columns=['timestamp', 'level', 'alarm_type', 'message', 
                        'value', 'threshold', 'acknowledged'])
        return []

    def query_oee(self, device_id: str, start_time: datetime, end_time: datetime) -> list[dict[str, Any]]:
        """查询OEE记录"""
        table_name = get_oee_table_name(device_id)
        safe_table = self._sanitize_identifier(table_name)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            SELECT ts, availability, performance, quality_rate, oee,
                   total_count, good_count, run_time, downtime
            FROM {safe_table}
            WHERE ts >= '{start_ts}' AND ts <= '{end_ts}'
            ORDER BY ts DESC
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self._bump_stat('queries', 1)
            return self._format_query_result(result,
                columns=['timestamp', 'availability', 'performance', 'quality_rate', 'oee',
                        'total_count', 'good_count', 'run_time', 'downtime'])
        return []

    def query_energy(self, device_id: str, start_time: datetime, end_time: datetime,
                     interval: str = "1h") -> list[dict[str, Any]]:
        """查询能源数据聚合"""
        # BUG-086：同 query_telemetry_agg —— interval 直接拼 SQL，必须先校验。
        if not re.match(r'^\d{1,6}[smhdwn]$', str(interval or ''), re.IGNORECASE):
            raise ValueError(f'非法 interval: {interval!r}')
        table_name = get_energy_table_name(device_id)
        safe_table = self._sanitize_identifier(table_name)

        start_ts = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_ts = end_time.strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            SELECT _wstart as ts, AVG(power) as avg_power,
                   MAX(power) as max_power, SUM(energy) as total_energy
            FROM {safe_table}
            WHERE ts >= '{start_ts}' AND ts <= '{end_ts}'
            INTERVAL({interval})
            ORDER BY ts
        """

        result = self._execute_sql(sql)
        if result and result.get('data'):
            self._bump_stat('queries', 1)
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
        with self._lock_ctx('_stats_lock'):
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
