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

import logging

from .base_gateway import BaseGateway
from .modbus_gateway import ModbusGateway

_logger = logging.getLogger(__name__)


def _optional_gateway(module: str, name: str):
    """可选协议依赖缺失时不让整个 gateway 包导入失败。"""
    try:
        mod = __import__(f'{__name__}.{module}', fromlist=[name])
        return getattr(mod, name)
    except Exception as e:      # noqa: BLE001 —— 缺依赖/协议栈异常都降级
        _logger.warning("协议网关 %s 不可用（导入失败）: %s", name, e)
        return None


S7Gateway = _optional_gateway('s7_gateway', 'S7Gateway')
OPCUAGateway = _optional_gateway('opcua_gateway', 'OPCUAGateway')
IEC104Gateway = _optional_gateway('iec104_gateway', 'IEC104Gateway')
DNP3Gateway = _optional_gateway('dnp3_gateway', 'DNP3Gateway')
try:
    from .mqtt_subscriber import MQTTSubscriber, MQTTDataDistributor
except Exception as e:      # noqa: BLE001
    _logger.warning("MQTT 订阅器不可用（导入失败）: %s", e)
    MQTTSubscriber = MQTTDataDistributor = None

# 版本号唯一真源：项目根目录 VERSION 文件
try:
    from pathlib import Path as _Path
    __version__ = (_Path(__file__).resolve().parent.parent / 'VERSION').read_text(encoding='utf-8').strip()
except Exception as e:      # noqa: BLE001
    _logger.warning("读取 VERSION 失败，版本号降级为 0.0.0: %s", e)
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
