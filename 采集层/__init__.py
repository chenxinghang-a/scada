"""
数据采集模块
支持协议: Modbus TCP/RTU, OPC UA, MQTT, REST HTTP
"""

import logging

from .modbus_client import ModbusClient
from .mqtt_client import MQTTClient
from .data_collector import DataCollector
from .device_manager import DeviceManager
from .rest_client import RESTDeviceClient

logger = logging.getLogger(__name__)

__all__ = ['ModbusClient', 'MQTTClient', 'DataCollector', 'DeviceManager', 'RESTDeviceClient']

# OPC UA可选导入（依赖opcua-asyncio）
try:
    from .opcua_client import OPCUAClient
    __all__.append('OPCUAClient')
except ImportError as e:
    # 安全忽略：opcua-asyncio 是可选依赖，未安装时仅禁用 OPC UA 协议支持，
    # 其余协议（Modbus/MQTT/REST）不受影响。记录日志便于排查"设备建了但采不到数"。
    logger.debug(f"OPC UA 可选依赖未安装，OPCUAClient 不可用（仅影响 OPC UA 协议）: {e}")
