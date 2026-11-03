"""模拟服务端：让「模拟模式」也跑在真实协议线上。

模块分工：

* ``device_data_hub`` —— 数据中枢。复用 ``采集层.device_behavior_simulator``
  造数，把工程值编码成寄存器/线圈，是所有协议服务端共用的唯一数据源。
* ``modbus_slave_pool`` —— Modbus TCP 从站池。每台设备一个真从站、一个端口。

新增其它协议（OPC UA / REST / MQTT）时，在自己的模块里接同一个 ``DeviceDataHub``，
并把类追加到下面的导出里 —— 不要在协议模块内部再实现一套造数逻辑，
否则模拟模式会出现两套互相矛盾的"物理定律"。
"""

from .device_data_hub import DeviceDataHub
from .modbus_slave_pool import ModbusSlavePool

# 其它协议的服务端：和 Modbus 从站池共用同一个 DeviceDataHub，不另造数据源。
from .mqtt_broker import MqttSimBroker
from .opcua_server import OpcuaSimServer
from .rest_gateway import RestSimGateway

__all__ = [
    'DeviceDataHub',
    'ModbusSlavePool',
    'MqttSimBroker',
    'OpcuaSimServer',
    'RestSimGateway',
]
