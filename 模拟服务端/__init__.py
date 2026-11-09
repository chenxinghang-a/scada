"""模拟服务端：让「模拟模式」也跑在真实协议线上。

模块分工：

* ``device_data_hub`` —— 数据中枢。复用 ``采集层.device_behavior_simulator``
  造数，把工程值编码成寄存器/线圈，是所有协议服务端共用的唯一数据源。
* ``modbus_slave_pool`` —— Modbus TCP 从站池。每台设备一个真从站、一个端口。

新增其它协议（OPC UA / REST / MQTT）时，在自己的模块里接同一个 ``DeviceDataHub``，
并把类追加到下面的导出里 —— 不要在协议模块内部再实现一套造数逻辑，
否则模拟模式会出现两套互相矛盾的"物理定律"。
"""

import logging

logger = logging.getLogger(__name__)

from .device_data_hub import DeviceDataHub
from .modbus_slave_pool import ModbusSlavePool


def _unavailable(name: str, exc: BaseException):
    """构造一个"可选依赖缺失"的占位类：实例化时给出可读错误。

    整个包不能在导入期失败 —— Modbus 从站池不依赖 paho/asyncua，
    只为缺一个可选依赖就让 `import 模拟服务端` 全崩，等于把能用的服务也停掉。
    """

    class _Unavailable:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(f'{name} 不可用（缺少可选依赖）: {exc}')

    _Unavailable.__name__ = name
    return _Unavailable


# 其它协议的服务端：和 Modbus 从站池共用同一个 DeviceDataHub，不另造数据源。
# 三者各自依赖可选库（paho-mqtt / asyncua / requests），必须逐个兜底导入。
try:
    from .mqtt_broker import MqttSimBroker
except ImportError as _e:      # pragma: no cover —— 取决于环境
    logger.warning('模拟 MQTT Broker 不可用（缺少 paho-mqtt）: %s', _e)
    MqttSimBroker = _unavailable('MqttSimBroker', _e)

try:
    from .opcua_server import OpcuaSimServer
except ImportError as _e:      # pragma: no cover
    logger.warning('模拟 OPC UA 服务端不可用（缺少 asyncua）: %s', _e)
    OpcuaSimServer = _unavailable('OpcuaSimServer', _e)

try:
    from .rest_gateway import RestSimGateway
except ImportError as _e:      # pragma: no cover
    logger.warning('模拟 REST 网关不可用: %s', _e)
    RestSimGateway = _unavailable('RestSimGateway', _e)

__all__ = [
    'DeviceDataHub',
    'ModbusSlavePool',
    'MqttSimBroker',
    'OpcuaSimServer',
    'RestSimGateway',
]
