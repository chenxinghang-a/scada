"""
数据采集模块
支持协议: Modbus TCP/RTU, OPC UA, MQTT, REST HTTP
"""

from .modbus_client import ModbusClient
from .mqtt_client import MQTTClient
from .data_collector import DataCollector
from .device_manager import DeviceManager
from .rest_client import RESTDeviceClient

__all__ = ['ModbusClient', 'MQTTClient', 'DataCollector', 'DeviceManager', 'RESTDeviceClient']

# OPC UA可选导入（依赖opcua-asyncio）
try:
    from .opcua_client import OPCUAClient
    __all__.append('OPCUAClient')
except ImportError:
    pass
