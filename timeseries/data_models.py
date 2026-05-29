"""
TDengine数据模型定义

定义时序数据库的表结构和数据模型。

TDengine核心概念：
1. 超级表 (STable)：同一类设备的模板，包含标签(TAG)和列(COLUMN)
2. 子表 (Table)：每个设备一张表，继承超级表结构
3. 标签 (TAG)：设备的静态属性（如设备ID、位置、类型）
4. 列 (COLUMN)：设备的动态数据（如温度、压力、状态）

设计原则：
- 每个设备的每个指标一张子表
- 使用超级表统一管理同类设备
- 标签用于设备分类和过滤
- 列用于存储时序数据
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _sanitize_table_name(raw: str) -> str:
    """防SQL注入：表名只保留字母数字下划线"""
    clean = re.sub(r'[^a-zA-Z0-9_]', '_', str(raw))
    # 确保不以数字开头
    if clean and clean[0].isdigit():
        clean = '_' + clean
    return clean


@dataclass
class TelemetryRecord:
    """
    遥测数据记录

    对应TDengine超级表：device_telemetry
    """
    device_id: str          # 设备ID
    register_name: str      # 寄存器名称
    timestamp: datetime     # 时间戳
    value: float            # 数据值
    quality: int = 192      # 数据质量（192=良好）
    unit: str = ""          # 单位
    protocol: str = ""      # 协议类型
    gateway_id: str = ""    # 网关ID

    def to_sql_values(self) -> str:
        """转换为SQL VALUES格式"""
        ts = self.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        return f"('{ts}', {self.value}, {self.quality})"

    def to_dict(self) -> dict[str, Any]:
        return {
            'device_id': self.device_id,
            'register_name': self.register_name,
            'timestamp': self.timestamp.isoformat(),
            'value': self.value,
            'quality': self.quality,
            'unit': self.unit,
            'protocol': self.protocol,
            'gateway_id': self.gateway_id
        }


@dataclass
class AlarmRecord:
    """
    报警记录

    对应TDengine超级表：alarm_records
    """
    alarm_id: str           # 报警ID
    device_id: str          # 设备ID
    timestamp: datetime     # 时间戳
    level: str              # 报警级别 (critical, warning, info)
    alarm_type: str         # 报警类型
    message: str            # 报警消息
    value: float = 0        # 实际值
    threshold: float = 0    # 阈值
    acknowledged: bool = False  # 是否已确认

    def to_sql_values(self) -> str:
        ts = self.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        ack = 1 if self.acknowledged else 0
        # 防SQL注入：转义反斜杠、单引号、空字节
        msg = self.message.replace("\\", "\\\\").replace("'", "\\'").replace("\x00", "")
        level = self.level.replace("\\", "\\\\").replace("'", "\\'").replace("\x00", "")
        alarm_type = self.alarm_type.replace("\\", "\\\\").replace("'", "\\'").replace("\x00", "")
        return f"('{ts}', '{level}', '{alarm_type}', '{msg}', {self.value}, {self.threshold}, {ack})"


@dataclass
class OEERecord:
    """
    OEE记录

    对应TDengine超级表：oee_records
    """
    device_id: str          # 设备ID
    timestamp: datetime     # 时间戳
    availability: float     # 可用率 (0-1)
    performance: float      # 性能率 (0-1)
    quality_rate: float     # 质量率 (0-1)
    oee: float              # OEE值 (0-1)
    total_count: int = 0    # 总产量
    good_count: int = 0     # 合格品数
    run_time: float = 0     # 运行时间（秒）
    downtime: float = 0     # 停机时间（秒）

    def to_sql_values(self) -> str:
        ts = self.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        return (f"('{ts}', {self.availability}, {self.performance}, "
                f"{self.quality_rate}, {self.oee}, {self.total_count}, "
                f"{self.good_count}, {self.run_time}, {self.downtime})")


@dataclass
class EnergyRecord:
    """
    能源记录

    对应TDengine超级表：energy_records
    """
    device_id: str          # 设备ID
    timestamp: datetime     # 时间戳
    power: float            # 功率 (kW)
    energy: float           # 累计电量 (kWh)
    voltage: float = 0      # 电压 (V)
    current: float = 0      # 电流 (A)
    power_factor: float = 1.0  # 功率因数

    def to_sql_values(self) -> str:
        ts = self.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        return (f"('{ts}', {self.power}, {self.energy}, "
                f"{self.voltage}, {self.current}, {self.power_factor})")


@dataclass
class PredictiveRecord:
    """
    预测性维护记录

    对应TDengine超级表：predictive_records
    """
    device_id: str          # 设备ID
    timestamp: datetime     # 时间戳
    health_score: float     # 健康评分 (0-100)
    failure_probability: float  # 故障概率 (0-1)
    remaining_life: float   # 剩余寿命（小时）
    anomaly_score: float = 0    # 异常分数
    trend: str = "stable"   # 趋势 (improving, stable, degrading)

    def to_sql_values(self) -> str:
        ts = self.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        trend = _escape_sql_value(self.trend)
        return (f"('{ts}', {self.health_score}, {self.failure_probability}, "
                f"{self.remaining_life}, {self.anomaly_score}, '{trend}')")


# TDengine超级表定义
STABLE_DEFINITIONS = {
    # 遥测数据超级表
    'device_telemetry': """
        CREATE STABLE IF NOT EXISTS device_telemetry (
            ts TIMESTAMP,
            value DOUBLE,
            quality INT
        ) TAGS (
            device_id NCHAR(64),
            register_name NCHAR(64),
            unit NCHAR(16),
            protocol NCHAR(32),
            gateway_id NCHAR(64)
        )
    """,

    # 报警记录超级表
    'alarm_records': """
        CREATE STABLE IF NOT EXISTS alarm_records (
            ts TIMESTAMP,
            level NCHAR(16),
            alarm_type NCHAR(32),
            message NCHAR(512),
            value DOUBLE,
            threshold DOUBLE,
            acknowledged INT
        ) TAGS (
            device_id NCHAR(64),
            alarm_id NCHAR(64)
        )
    """,

    # OEE记录超级表
    'oee_records': """
        CREATE STABLE IF NOT EXISTS oee_records (
            ts TIMESTAMP,
            availability DOUBLE,
            performance DOUBLE,
            quality_rate DOUBLE,
            oee DOUBLE,
            total_count BIGINT,
            good_count BIGINT,
            run_time DOUBLE,
            downtime DOUBLE
        ) TAGS (
            device_id NCHAR(64)
        )
    """,

    # 能源记录超级表
    'energy_records': """
        CREATE STABLE IF NOT EXISTS energy_records (
            ts TIMESTAMP,
            power DOUBLE,
            energy DOUBLE,
            voltage DOUBLE,
            current DOUBLE,
            power_factor DOUBLE
        ) TAGS (
            device_id NCHAR(64)
        )
    """,

    # 预测性维护记录超级表
    'predictive_records': """
        CREATE STABLE IF NOT EXISTS predictive_records (
            ts TIMESTAMP,
            health_score DOUBLE,
            failure_probability DOUBLE,
            remaining_life DOUBLE,
            anomaly_score DOUBLE,
            trend NCHAR(16)
        ) TAGS (
            device_id NCHAR(64)
        )
    """
}


def _escape_sql_value(val: str) -> str:
    """防SQL注入：转义字符串值"""
    return str(val).replace("\\", "\\\\").replace("'", "\\'").replace("\x00", "")


def get_create_table_sql(table_name: str, stable_name: str, tags: dict[str, str]) -> str:
    """
    生成创建子表的SQL

    Args:
        table_name: 子表名称
        stable_name: 超级表名称
        tags: 标签值 {tag_name: tag_value}
    """
    tag_values = ", ".join([f"'{_escape_sql_value(v)}'" for v in tags.values()])
    return f"CREATE TABLE IF NOT EXISTS {table_name} USING {stable_name} TAGS ({tag_values})"


def get_telemetry_table_name(device_id: str, register_name: str) -> str:
    """生成遥测数据子表名称"""
    clean_device = _sanitize_table_name(device_id)
    clean_register = _sanitize_table_name(register_name)
    return f"tel_{clean_device}_{clean_register}"


def get_alarm_table_name(device_id: str) -> str:
    """生成报警记录子表名称"""
    clean_device = _sanitize_table_name(device_id)
    return f"alarm_{clean_device}"


def get_oee_table_name(device_id: str) -> str:
    """生成OEE记录子表名称"""
    clean_device = _sanitize_table_name(device_id)
    return f"oee_{clean_device}"


def get_energy_table_name(device_id: str) -> str:
    """生成能源记录子表名称"""
    clean_device = _sanitize_table_name(device_id)
    return f"energy_{clean_device}"


def get_predictive_table_name(device_id: str) -> str:
    """生成预测性维护记录子表名称"""
    clean_device = _sanitize_table_name(device_id)
    return f"predict_{clean_device}"
