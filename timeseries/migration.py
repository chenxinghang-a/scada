"""
SQLite到TDengine数据迁移工具

将SQLite中的历史数据迁移到TDengine时序数据库。

迁移策略：
1. 读取SQLite中的历史数据
2. 转换为TDengine数据模型
3. 批量写入TDengine
4. 验证数据完整性

注意事项：
- 迁移前请备份SQLite数据库
- 迁移过程中不要写入新数据
- 迁移完成后验证数据一致性

表名与列映射（2026-09 修正）
---------------------------
原先这里读的是 ``telemetry`` / ``alarms`` 两张**根本不存在的表**
（``存储层/database.py`` 建的是 ``history_data`` / ``alarm_records``），
查询直接抛 ``no such table`` 被 catch 掉、统计里只剩 0 ——
即"迁移必然 0 行"的伪实现。现在表名收敛到下面两个常量，
列也不再假设 SQLite 侧有 ``quality`` / ``alarm_type``：

| TDengine 列 | SQLite 来源 |
|---|---|
| ``device_telemetry.value`` | ``history_data.value`` |
| ``device_telemetry.quality`` | 无来源，恒 192(GOOD)（质量码只存在 ``realtime_data.quality``，且只保留最新值，无法回填历史） |
| ``device_telemetry.unit`` | ``history_data.unit`` |
| ``alarm_records.alarm_type`` | 无来源，恒空串（SQLite 侧没有该列） |
"""

import sqlite3
import logging
from datetime import datetime
from typing import Any
from pathlib import Path

from .tdengine_client import TDengineClient
from .data_models import (
    TelemetryRecord, AlarmRecord, OEERecord, EnergyRecord
)


#: SQLite 侧的遥测源表（= 存储层的历史数据表，不要把名字改回 'telemetry'）
TELEMETRY_TABLE = 'history_data'

#: SQLite 侧的报警源表（不要把名字改回 'alarms'）
ALARM_TABLE = 'alarm_records'

#: SQLite 侧没有质量码列（质量码只存于 realtime_data 的最新值），
#: 历史行回填不了，统一按 GOOD 处理。
DEFAULT_QUALITY = 192

#: TDengine 侧的超级表名（与 ``data_models.STABLE_DEFINITIONS`` 保持一致）
TD_TELEMETRY_TABLE = 'device_telemetry'
TD_ALARM_TABLE = 'alarm_records'


def _parse_timestamp(raw: Any) -> datetime:
    """解析 SQLite 里的时间戳文本。

    ``存储层.database.adapt_datetime`` 写入的是空格分隔的
    ``'YYYY-MM-DD HH:MM:SS.ffffff'``（``datetime.isoformat(sep=' ')``），
    这里用 ``fromisoformat`` 解析；老数据/手工灌数可能是 ``'T'`` 分隔，
    ``fromisoformat`` 同样接受，无需分支。
    """
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw))


class SQLiteToTDengineMigrator:
    """
    SQLite到TDengine数据迁移器
    """

    def __init__(self, sqlite_path: str, tdengine_client: TDengineClient):
        """
        初始化迁移器

        Args:
            sqlite_path: SQLite数据库路径
            tdengine_client: TDengine客户端
        """
        self.sqlite_path = sqlite_path
        self.tdengine = tdengine_client
        self.logger = logging.getLogger("Migrator")

        # SQLite连接
        self._sqlite_conn: sqlite3.Connection | None = None

        # 迁移统计
        self.stats: dict[str, Any] = {
            'telemetry_migrated': 0,
            'alarms_migrated': 0,
            'oee_migrated': 0,
            'energy_migrated': 0,
            'errors': 0,
            'start_time': None,
            'end_time': None
        }

    def connect_sqlite(self) -> bool:
        """连接SQLite数据库"""
        try:
            if not Path(self.sqlite_path).exists():
                self.logger.error(f"SQLite数据库不存在: {self.sqlite_path}")
                return False

            self._sqlite_conn = sqlite3.connect(self.sqlite_path)
            self._sqlite_conn.row_factory = sqlite3.Row
            self.logger.info(f"SQLite连接成功: {self.sqlite_path}")
            return True
        except Exception as e:
            self.logger.error(f"SQLite连接失败: {e}")
            return False

    def disconnect_sqlite(self):
        """断开SQLite连接"""
        if self._sqlite_conn:
            self._sqlite_conn.close()
            self._sqlite_conn = None

    def migrate_all(self, batch_size: int = 1000) -> dict[str, Any]:
        """
        执行完整迁移

        Args:
            batch_size: 批量写入大小

        Returns:
            dict[str, Any]: 迁移统计
        """
        self.stats['start_time'] = datetime.now()

        try:
            # 连接数据库
            if not self.connect_sqlite():
                return self.stats

            # 连接TDengine
            if not self.tdengine.connect():
                return self.stats

            # 初始化TDengine表
            self.tdengine.init_tables()

            # 迁移各表数据
            self._migrate_telemetry(batch_size)
            self._migrate_alarms(batch_size)
            self._migrate_oee(batch_size)
            self._migrate_energy(batch_size)

            self.stats['end_time'] = datetime.now()

            # 打印统计
            self._print_stats()

            return self.stats

        except Exception as e:
            self.logger.error(f"迁移异常: {e}")
            self.stats['errors'] += 1
            return self.stats
        finally:
            self.disconnect_sqlite()

    def _table_exists(self, cursor: sqlite3.Cursor, table: str) -> bool:
        """判断源库里是否存在某张表（迁移器可指向任意库文件，缺表要跳过而不是报错）"""
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cursor.fetchone() is not None

    def _migrate_telemetry(self, batch_size: int):
        """迁移遥测数据（源表：``history_data``）"""
        self.logger.info("开始迁移遥测数据...")

        try:
            cursor = self._sqlite_conn.cursor()

            if not self._table_exists(cursor, TELEMETRY_TABLE):
                self.logger.info(f"遥测源表 {TELEMETRY_TABLE} 不存在，跳过迁移")
                return

            # 查询SQLite中的遥测数据
            # 列固定为 5 个真实存在的列：history_data 没有 quality 列，
            # 质量码统一用 DEFAULT_QUALITY 回填（见模块 docstring 的映射表）。
            cursor.execute(f"""
                SELECT device_id, register_name, timestamp, value, unit
                FROM {TELEMETRY_TABLE}
                ORDER BY timestamp
            """)

            batch = []
            for row in cursor:
                try:
                    record = TelemetryRecord(
                        device_id=row['device_id'],
                        register_name=row['register_name'],
                        timestamp=_parse_timestamp(row['timestamp']),
                        value=row['value'],
                        quality=DEFAULT_QUALITY,
                        unit=row['unit'] or ''
                    )
                    batch.append(record)

                    if len(batch) >= batch_size:
                        self.tdengine.write_telemetry_batch(batch)
                        self.stats['telemetry_migrated'] += len(batch)
                        self.logger.info(f"已迁移 {self.stats['telemetry_migrated']} 条遥测数据")
                        batch = []

                except Exception as e:
                    self.logger.error(f"处理遥测数据异常: {e}")
                    self.stats['errors'] += 1

            # 写入剩余数据
            if batch:
                self.tdengine.write_telemetry_batch(batch)
                self.stats['telemetry_migrated'] += len(batch)

            self.logger.info(f"遥测数据迁移完成: {self.stats['telemetry_migrated']} 条")

        except Exception as e:
            self.logger.error(f"迁移遥测数据失败: {e}")
            self.stats['errors'] += 1

    def _migrate_alarms(self, batch_size: int):
        """迁移报警数据（源表：``alarm_records``）"""
        self.logger.info("开始迁移报警数据...")

        try:
            cursor = self._sqlite_conn.cursor()

            if not self._table_exists(cursor, ALARM_TABLE):
                self.logger.info(f"报警源表 {ALARM_TABLE} 不存在，跳过迁移")
                return

            # 查询SQLite中的报警数据
            # alarm_records 没有 alarm_type 列（TDengine 超级表有）→ 传空串；
            # 也没有 register_name 对应字段，故不映射。
            cursor.execute(f"""
                SELECT alarm_id, device_id, timestamp, alarm_level,
                       alarm_message, threshold, actual_value
                FROM {ALARM_TABLE}
                ORDER BY timestamp
            """)

            count = 0
            for row in cursor:
                try:
                    record = AlarmRecord(
                        alarm_id=row['alarm_id'],
                        device_id=row['device_id'],
                        timestamp=_parse_timestamp(row['timestamp']),
                        level=row['alarm_level'],
                        alarm_type='',
                        message=row['alarm_message'] or '',
                        value=row['actual_value'] if row['actual_value'] else 0,
                        threshold=row['threshold'] if row['threshold'] else 0
                    )
                    self.tdengine.write_alarm(record)
                    count += 1

                    if count % batch_size == 0:
                        self.stats['alarms_migrated'] = count
                        self.logger.info(f"已迁移 {count} 条报警数据")

                except Exception as e:
                    self.logger.error(f"处理报警数据异常: {e}")
                    self.stats['errors'] += 1

            self.stats['alarms_migrated'] = count
            self.logger.info(f"报警数据迁移完成: {count} 条")

        except Exception as e:
            self.logger.error(f"迁移报警数据失败: {e}")
            self.stats['errors'] += 1

    def _migrate_oee(self, batch_size: int):
        """迁移OEE数据"""
        self.logger.info("开始迁移OEE数据...")

        try:
            cursor = self._sqlite_conn.cursor()

            # 检查OEE表是否存在
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='oee_records'
            """)

            if not cursor.fetchone():
                self.logger.info("OEE表不存在，跳过迁移")
                return

            cursor.execute("""
                SELECT device_id, timestamp, availability, performance, 
                       quality_rate, oee, total_count, good_count
                FROM oee_records
                ORDER BY timestamp
            """)

            count = 0
            for row in cursor:
                try:
                    record = OEERecord(
                        device_id=row['device_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        availability=row['availability'],
                        performance=row['performance'],
                        quality_rate=row['quality_rate'],
                        oee=row['oee'],
                        total_count=row['total_count'] if row['total_count'] else 0,
                        good_count=row['good_count'] if row['good_count'] else 0
                    )
                    self.tdengine.write_oee(record)
                    count += 1

                    if count % batch_size == 0:
                        self.stats['oee_migrated'] = count
                        self.logger.info(f"已迁移 {count} 条OEE数据")

                except Exception as e:
                    self.logger.error(f"处理OEE数据异常: {e}")
                    self.stats['errors'] += 1

            self.stats['oee_migrated'] = count
            self.logger.info(f"OEE数据迁移完成: {count} 条")

        except Exception as e:
            self.logger.error(f"迁移OEE数据失败: {e}")
            self.stats['errors'] += 1

    def _migrate_energy(self, batch_size: int):
        """迁移能源数据"""
        self.logger.info("开始迁移能源数据...")

        try:
            cursor = self._sqlite_conn.cursor()

            # 检查能源表是否存在
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='energy_records'
            """)

            if not cursor.fetchone():
                self.logger.info("能源表不存在，跳过迁移")
                return

            cursor.execute("""
                SELECT device_id, timestamp, power, energy, voltage, current
                FROM energy_records
                ORDER BY timestamp
            """)

            count = 0
            for row in cursor:
                try:
                    record = EnergyRecord(
                        device_id=row['device_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        power=row['power'],
                        energy=row['energy'],
                        voltage=row['voltage'] if row['voltage'] else 0,
                        current=row['current'] if row['current'] else 0
                    )
                    self.tdengine.write_energy(record)
                    count += 1

                    if count % batch_size == 0:
                        self.stats['energy_migrated'] = count
                        self.logger.info(f"已迁移 {count} 条能源数据")

                except Exception as e:
                    self.logger.error(f"处理能源数据异常: {e}")
                    self.stats['errors'] += 1

            self.stats['energy_migrated'] = count
            self.logger.info(f"能源数据迁移完成: {count} 条")

        except Exception as e:
            self.logger.error(f"迁移能源数据失败: {e}")
            self.stats['errors'] += 1

    def _print_stats(self):
        """打印迁移统计"""
        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()

        self.logger.info("=" * 50)
        self.logger.info("数据迁移完成")
        self.logger.info("=" * 50)
        self.logger.info(f"遥测数据: {self.stats['telemetry_migrated']} 条")
        self.logger.info(f"报警数据: {self.stats['alarms_migrated']} 条")
        self.logger.info(f"OEE数据: {self.stats['oee_migrated']} 条")
        self.logger.info(f"能源数据: {self.stats['energy_migrated']} 条")
        self.logger.info(f"错误数: {self.stats['errors']}")
        self.logger.info(f"耗时: {duration:.2f} 秒")
        self.logger.info("=" * 50)

    @staticmethod
    def _extract_scalar(result: Any) -> Any:
        """从 TDengine 查询结果里取出第一个标量值。

        兼容两种返回形态（客户端 REST/原生两条路径的包装不同）：
        - REST: ``{'code': 0, 'data': [{'column_meta': [...], 'data': [[3]]}]}``
        - 原生: ``{'data': [[3]]}``

        识别不出来时返回 None（表示"没查到/不可用"，而不是 0）。
        """
        if not result:
            return None
        data = result.get('data')
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0].get('data')
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, (list, tuple)):
                return first[0] if first else None
            return first
        return None

    def _count_tdengine(self, table: str) -> int | None:
        """向 TDengine 求证某超级表的行数；无法求证时返回 None。"""
        executor = getattr(self.tdengine, '_execute_sql', None)
        if executor is None:
            return None
        try:
            count = self._extract_scalar(executor(f'SELECT COUNT(*) FROM {table}'))
            return int(count) if count is not None else None
        except Exception as e:
            self.logger.debug(f"TDengine 行数查询失败({table}): {e}")
            return None

    def verify_migration(self, sample_size: int = 100) -> dict[str, Any]:
        """
        验证迁移结果

        两侧都取到行数时才判 ``match``；TDengine 侧查不到（未连接/无查询能力）
        时 ``tdengine_count`` 与 ``match`` 为 None，表示"**未校验**" ——
        以前这里恒为 ``(0, False)``，会因为"没查"而报"不一致"，
        属于会误导运维的假信号。

        Args:
            sample_size: 抽样检查数量（当前仅做行数核对，保留参数以兼容调用方）

        Returns:
            dict[str, Any]: 各表 ``{sqlite_count, tdengine_count, match}``
        """
        self.logger.info("开始验证迁移结果...")

        results = {
            'telemetry': {'sqlite_count': 0, 'tdengine_count': None, 'match': None},
            'alarms': {'sqlite_count': 0, 'tdengine_count': None, 'match': None},
            'oee': {'sqlite_count': 0, 'tdengine_count': None, 'match': None},
            'energy': {'sqlite_count': 0, 'tdengine_count': None, 'match': None}
        }

        # SQLite 源表 -> TDengine 超级表；None 表示可选表（源库可能没有）
        tables = {
            'telemetry': (TELEMETRY_TABLE, TD_TELEMETRY_TABLE),
            'alarms': (ALARM_TABLE, TD_ALARM_TABLE),
            'oee': ('oee_records', 'oee_records'),
            'energy': ('energy_records', 'energy_records'),
        }

        try:
            cursor = self._sqlite_conn.cursor()

            for key, (sqlite_table, td_table) in tables.items():
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {sqlite_table}")
                    results[key]['sqlite_count'] = cursor.fetchone()[0]
                except Exception as e:
                    # 可选表（oee/energy）在旧库里可能不存在；计数保持 0，match 保持"未校验"
                    self.logger.debug(f"验证 {sqlite_table} 跳过（表不存在或查询失败）: {e}")
                    continue

                td_count = self._count_tdengine(td_table)
                results[key]['tdengine_count'] = td_count
                if td_count is not None:
                    results[key]['match'] = (td_count == results[key]['sqlite_count'])

            self.logger.info("验证完成")
            return results

        except Exception as e:
            self.logger.error(f"验证失败: {e}")
            return results


# 命令行工具
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='SQLite到TDengine数据迁移工具')
    parser.add_argument('--sqlite', type=str, required=True,
                        help='SQLite数据库路径')
    parser.add_argument('--tdengine-host', type=str, default='localhost',
                        help='TDengine主机地址')
    parser.add_argument('--tdengine-port', type=int, default=6041,
                        help='TDengine端口')
    parser.add_argument('--batch-size', type=int, default=1000,
                        help='批量写入大小')
    parser.add_argument('--verify', action='store_true',
                        help='验证迁移结果')

    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 创建TDengine客户端
    tdengine = TDengineClient(
        host=args.tdengine_host,
        port=args.tdengine_port
    )

    # 创建迁移器
    migrator = SQLiteToTDengineMigrator(args.sqlite, tdengine)

    # 执行迁移
    stats = migrator.migrate_all(batch_size=args.batch_size)

    # 验证结果
    if args.verify:
        migrator.connect_sqlite()
        results = migrator.verify_migration()
        migrator.disconnect_sqlite()
