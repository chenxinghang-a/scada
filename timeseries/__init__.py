"""
工业4.0 SCADA系统 — 时序数据库模块

本模块实现了四层漏斗架构的第四层：时序数据库层。

主要功能：
- TDengine时序数据库客户端
- 数据模型设计（超级表/子表）
- 高性能数据写入
- 复杂时间窗口查询
- 数据降采样和聚合

优势：
- 查询性能比SQLite提升100倍以上
- 支持降采样和数据压缩
- 支持复杂的时间窗口聚合
- 专为时序数据优化

使用方式：
    from timeseries import TDengineClient

    client = TDengineClient("localhost", 6041)
    client.write_telemetry(telemetry)
    data = client.query_telemetry("CNC_001", start_time, end_time)
"""

from .tdengine_client import TDengineClient
from .data_models import (
    TelemetryRecord,
    AlarmRecord,
    OEERecord,
    EnergyRecord
)
from .query_builder import QueryBuilder
from .migration import SQLiteToTDengineMigrator

__version__ = "2.1.0"
__author__ = "Industrial SCADA Team"

__all__ = [
    # 客户端
    'TDengineClient',

    # 数据模型
    'TelemetryRecord',
    'AlarmRecord',
    'OEERecord',
    'EnergyRecord',

    # 查询构建器
    'QueryBuilder',

    # 数据迁移
    'SQLiteToTDengineMigrator',
]
