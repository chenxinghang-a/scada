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
from .mqtt_subscriber import MQTTSubscriber, MQTTDataDistributor

__version__ = "2.1.0"
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

    # MQTT订阅
    'MQTTSubscriber',
    'MQTTDataDistributor',
]
