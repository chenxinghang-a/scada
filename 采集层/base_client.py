"""
设备客户端抽象基类
定义所有协议客户端（Modbus/OPC UA/MQTT/REST）的统一接口
模拟客户端和真实客户端都必须继承此基类
"""

from abc import ABC, abstractmethod
from typing import Any, Callable


class BaseDeviceClient(ABC):
    """
    设备客户端基类

    所有协议客户端（模拟/真实）的统一接口定义。
    通过抽象基类强制接口一致性，编译期即可发现接口不匹配。
    """

    def __init__(self, config: dict[str, Any]):
        """
        初始化客户端

        Args:
            config: 设备配置字典，包含 device_id, device_name, protocol 等字段
        """
        self.config = config
        self.device_id = config.get('device_id', 'unknown')
        self.device_name = config.get('device_name', self.device_id)
        self.protocol = config.get('protocol', 'unknown')
        self.connected = False

    @abstractmethod
    def connect(self) -> bool:
        """
        建立设备连接

        Returns:
            bool: 连接是否成功
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """断开设备连接"""
        pass

    @abstractmethod
    def get_stats(self) -> dict[str, Any]:
        """
        获取客户端统计信息

        Returns:
            dict[str, Any]: 包含 device_id, device_name, connected 等字段的统计字典
        """
        pass

    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        """
        获取最新数据缓存

        适用于 OPC UA / MQTT / REST 等有缓存机制的客户端。
        Modbus 客户端默认返回空字典（需要轮询读取）。

        Returns:
            dict[str, Any]: {register_name: {'value': ..., 'unit': ..., 'timestamp': ...}}
        """
        return {}

    def add_data_callback(self, callback: Callable[..., Any]) -> None:
        """
        添加数据回调函数

        适用于 OPC UA / MQTT / REST 等推送型客户端。
        回调签名: callback(device_id: str, name: str, value: Any, unit: str)

        Args:
            callback: 回调函数
        """
        pass


class ModbusClientInterface(BaseDeviceClient):
    """Modbus协议客户端接口"""

    @abstractmethod
    def read_holding_registers(self, address: int, count: int,
                              slave_id: int | None = None) -> list[int] | None:
        """读取保持寄存器"""
        pass

    @abstractmethod
    def read_input_registers(self, address: int, count: int,
                            slave_id: int | None = None) -> list[int] | None:
        """读取输入寄存器"""
        pass

    @abstractmethod
    def read_coils(self, address: int, count: int,
                  slave_id: int | None = None) -> list[bool] | None:
        """读取线圈状态"""
        pass

    @abstractmethod
    def read_discrete_inputs(self, address: int, count: int,
                            slave_id: int | None = None) -> list[bool] | None:
        """读取离散输入"""
        pass

    @abstractmethod
    def write_single_register(self, address: int, value: int,
                             slave_id: int | None = None) -> bool:
        """写入单个寄存器"""
        pass

    @abstractmethod
    def write_single_coil(self, address: int, value: bool,
                         slave_id: int | None = None) -> bool:
        """写入单个线圈"""
        pass

    # 数据解码方法（有默认实现）
    def decode_float32(self, registers: list[int]) -> float:
        """解码32位浮点数（大端序）"""
        import struct
        raw = struct.pack('>HH', registers[0], registers[1])
        return struct.unpack('>f', raw)[0]

    def decode_float64(self, registers: list[int]) -> float:
        """解码64位浮点数（大端序）"""
        import struct
        raw = struct.pack('>HHHH', registers[0], registers[1], registers[2], registers[3])
        return struct.unpack('>d', raw)[0]

    def decode_uint16(self, register: int) -> int:
        """解码16位无符号整数"""
        return register & 0xFFFF

    def decode_int16(self, register: int) -> int:
        """解码16位有符号整数"""
        if register > 32767:
            return register - 65536
        return register

    def decode_uint32(self, registers: list[int]) -> int:
        """解码32位无符号整数"""
        return (registers[0] << 16) | registers[1]

    def decode_int32(self, registers: list[int]) -> int:
        """解码32位有符号整数"""
        value = (registers[0] << 16) | registers[1]
        if value > 2147483647:
            return value - 4294967296
        return value


class PushClientInterface(BaseDeviceClient):
    """推送型客户端接口（OPC UA / MQTT / REST）"""

    @abstractmethod
    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        """获取最新数据缓存"""
        pass

    @abstractmethod
    def add_data_callback(self, callback: Callable[..., Any]) -> None:
        """添加数据回调函数"""
        pass
