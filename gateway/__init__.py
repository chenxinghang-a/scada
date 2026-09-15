"""
工业4.0 SCADA系统 — 协议网关服务

本模块实现了四层漏斗架构的第一层：边缘网关层。

主要功能：
- 多协议支持（Modbus、S7、OPC UA、MQTT）
- 统一物模型转换
- MQTT消息发布
- 独立进程运行，故障隔离

使用方式：
    from gateway import ModbusGateway

    config = {...}
    gateway = ModbusGateway(config)
    gateway.start()
"""

from .thing_model import (
    DeviceTelemetry,
    DeviceStatus,
    AlarmMessage,
    ThingModelConverter,
    ThingModelValidator,
    MQTTTopics,
    ProtocolType,
    DataQuality,
    MetricValue
)

from .base_gateway import BaseGateway
from .modbus_gateway import ModbusGateway
from .s7_gateway import S7Gateway
from .opcua_gateway import OPCUAGateway
from .iec104_gateway import IEC104Gateway
from .dnp3_gateway import DNP3Gateway
from .mqtt_subscriber import MQTTSubscriber, MQTTDataDistributor

# 版本号唯一真源：项目根目录 VERSION 文件
try:
    from pathlib import Path as _Path
    __version__ = (_Path(__file__).resolve().parent.parent / 'VERSION').read_text(encoding='utf-8').strip()
except Exception:
    __version__ = "0.0.0"
__author__ = "Industrial SCADA Team"

__all__ = [
    # 物模型
    'DeviceTelemetry',
    'DeviceStatus',
    'AlarmMessage',
    'ThingModelConverter',
    'ThingModelValidator',
    'MQTTTopics',
    'ProtocolType',
    'DataQuality',
    'MetricValue',

    # 网关基类
    'BaseGateway',

    # 协议网关
    'ModbusGateway',
    'S7Gateway',
    'OPCUAGateway',
    'IEC104Gateway',
    'DNP3Gateway',

    # MQTT订阅
    'MQTTSubscriber',
    'MQTTDataDistributor',
]
