"""
设备客户端抽象基类
定义所有协议客户端（Modbus/OPC UA/MQTT/REST）的统一接口
模拟客户端和真实客户端都必须继承此基类
"""

import math
import struct
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable


class ByteOrder(Enum):
    """寄存器型协议（Modbus/MC/FINS）的 32/64 位值字节序。

    与 ``modbus_client.ByteOrder`` 保持语义一致；此处独立定义以避免
    与采集层具体实现产生循环依赖，供 MCClient / FINSClient 复用。
    """
    ABCD = 'ABCD'   # Big-endian（西门子 S7-1200/1500 默认）
    BADC = 'BADC'   # Big-endian，字内字节交换
    CDAB = 'CDAB'   # Little-endian 字序（西门子 S7-300/400）
    DCBA = 'DCBA'   # Little-endian（部分三菱）


class ByteOrderCapableDecoder:
    """为寄存器型客户端提供「按设备配置 byte_order 解码」的 mixin。

    原本 base_client 的 decode_* 硬编码 ABCD，导致非 ABCD 设备解码错位。
    本 mixin 与 ``modbus_client.ModbusClient`` 的字节序逻辑严格一致，
    供 MCClient / FINSClient 继承，使 ``_collect_modbus`` 不再因协议差异
    抛出 AttributeError，且能正确解码四种字节序。
    """

    def _resolve_byte_order(self) -> ByteOrder:
        cfg = getattr(self, 'config', None) or {}
        raw = str(cfg.get('byte_order', 'ABCD')).upper()
        try:
            return ByteOrder(raw)
        except ValueError:
            return ByteOrder.ABCD

    def _reorder_32(self, w1: int, w2: int) -> int:
        """把两个 16-bit 寄存器按字节序重排为 32-bit 整数（ABCD 等价 (w1<<16)|w2）。"""
        bo = self._resolve_byte_order()
        if bo == ByteOrder.BADC:
            # 字内字节交换，再拼接
            w1 = ((w1 & 0xFF) << 8) | ((w1 >> 8) & 0xFF)
            w2 = ((w2 & 0xFF) << 8) | ((w2 >> 8) & 0xFF)
            return (w1 << 16) | w2
        if bo == ByteOrder.CDAB:
            # 仅字交换
            return (w2 << 16) | w1
        if bo == ByteOrder.DCBA:
            # 完全小端：字序反转 + 字内字节交换
            b0 = (w1 >> 8) & 0xFF
            b1 = w1 & 0xFF
            b2 = (w2 >> 8) & 0xFF
            b3 = w2 & 0xFF
            return (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
        return (w1 << 16) | w2

    def decode_float32(self, registers: list[int]) -> float | None:
        """解码 32 位浮点数，支持四种字节序；NaN/Inf 返回 None。"""
        if len(registers) < 2:
            raise ValueError("需要至少2个寄存器")
        raw = self._reorder_32(registers[0], registers[1]) & 0xFFFFFFFF
        value = struct.unpack('!f', struct.pack('!I', raw))[0]
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    def decode_float64(self, registers: list[int]) -> float | None:
        """解码 64 位浮点数（四个寄存器），支持四种字节序；NaN/Inf 返回 None。"""
        if len(registers) < 4:
            raise ValueError("需要至少4个寄存器")
        bo = self._resolve_byte_order()
        if bo == ByteOrder.BADC:
            raw = struct.pack('>HHHH', registers[1], registers[0], registers[3], registers[2])
        elif bo == ByteOrder.CDAB:
            raw = struct.pack('>HHHH', registers[2], registers[3], registers[0], registers[1])
        elif bo == ByteOrder.DCBA:
            # 每个寄存器内字节交换，且寄存器顺序也反转（完全小端）
            def _swap16(w: int) -> int:
                return ((w & 0xFF) << 8) | ((w >> 8) & 0xFF)
            b0 = _swap16(registers[0])
            b1 = _swap16(registers[1])
            b2 = _swap16(registers[2])
            b3 = _swap16(registers[3])
            raw = struct.pack('>HHHH', b3, b2, b1, b0)
        else:  # ABCD
            raw = struct.pack('>HHHH', registers[0], registers[1], registers[2], registers[3])
        value = struct.unpack('>d', raw)[0]
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    def decode_uint16(self, register: int) -> int:
        """解码 16 位无符号整数（单寄存器，字节序固定）。"""
        return register & 0xFFFF

    def decode_int16(self, register: int) -> int:
        """解码 16 位有符号整数（单寄存器，字节序固定）。"""
        if register & 0x8000:
            return register - 0x10000
        return register

    def decode_uint32(self, registers: list[int]) -> int:
        """解码 32 位无符号整数，支持四种字节序。"""
        return self._reorder_32(registers[0], registers[1]) & 0xFFFFFFFF

    def decode_int32(self, registers: list[int]) -> int:
        """解码 32 位有符号整数，支持四种字节序。"""
        raw = self._reorder_32(registers[0], registers[1])
        if raw & 0x80000000:
            return raw - 0x100000000
        return raw


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
            config: 设备配置字典，包含 id/device_id, name/device_name, protocol 等字段
        """
        self.config = config
        # 兼容 'id' 和 'device_id' 两种配置格式
        self.device_id = config.get('device_id', config.get('id', 'unknown'))
        self.device_name = config.get('device_name', config.get('name', self.device_id))
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


class ModbusClientInterface(ByteOrderCapableDecoder, BaseDeviceClient):
    """Modbus协议客户端接口（继承字节序解码能力，子类可按需覆盖）"""

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

    def write_multiple_coils(self, address: int, values: list[bool],
                              slave_id: int | None = None) -> bool:
        """写入多个线圈（功能码15）"""
        raise NotImplementedError

    def write_multiple_registers(self, address: int, values: list[int],
                                  slave_id: int | None = None) -> bool:
        """写入多个寄存器（功能码16）"""
        raise NotImplementedError

    def read_write_multiple_registers(self, read_address: int, read_count: int,
                                       write_address: int, write_values: list[int],
                                       slave_id: int | None = None) -> list[int] | None:
        """读写多个寄存器（功能码23）"""
        raise NotImplementedError


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
