"""
Modbus客户端模块
实现Modbus TCP/RTU协议通信
"""

import math
import time
import struct
import logging
import threading
from typing import Any
from pymodbus.client import ModbusTcpClient, ModbusSerialClient
from pymodbus.exceptions import ModbusException, ConnectionException

logger = logging.getLogger(__name__)


class ModbusClient:
    """
    Modbus客户端封装类
    支持Modbus TCP和RTU协议
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.device_id = config.get('id')
        self.device_name = config.get('name')
        self.protocol = config.get('protocol', 'modbus_tcp')
        self.slave_id = config.get('slave_id', 1)

        # 客户端实例
        self.client = None
        self.connected = False

        # 连接参数
        if self.protocol == 'modbus_tcp':
            self.host = config.get('host', '127.0.0.1')
            self.port = config.get('port', 502)
        else:  # modbus_rtu
            self.serial_port = config.get('serial_port', '/dev/ttyUSB0')
            self.baudrate = config.get('baudrate', 9600)
            self.parity = config.get('parity', 'N')
            self.stopbits = config.get('stopbits', 1)
            self.bytesize = config.get('bytesize', 8)

        # 重连参数
        self._reconnect_attempts = 0
        self._max_reconnect_delay = 60  # 最大退避 60 秒
        self._last_reconnect_time = 0

        # 统计信息（线程安全）
        self._stats_lock = threading.Lock()
        self.stats: dict[str, Any] = {
            'total_reads': 0,
            'successful_reads': 0,
            'failed_reads': 0,
            'last_read_time': None,
            'last_error': None
        }

    def _inc_stat(self, key: str):
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    def connect(self) -> bool:
        """建立Modbus连接"""
        try:
            if self.protocol == 'modbus_tcp':
                self.client = ModbusTcpClient(
                    host=self.host,
                    port=self.port,
                    timeout=3  # 3秒超时，不是10秒
                )
            else:  # modbus_rtu
                self.client = ModbusSerialClient(
                    port=self.serial_port,
                    baudrate=self.baudrate,
                    parity=self.parity,
                    stopbits=self.stopbits,
                    bytesize=self.bytesize,
                    timeout=2  # RTU 2秒超时
                )

            self.connected = self.client.connect()

            if self.connected:
                self._reconnect_attempts = 0
                logger.info(f"设备 {self.device_name} 连接成功")
            else:
                logger.error(f"设备 {self.device_name} 连接失败")

            return self.connected

        except Exception as e:
            logger.error(f"设备 {self.device_name} 连接异常: {e}")
            with self._stats_lock:
                self.stats['last_error'] = str(e)
            return False

    def reconnect(self) -> bool:
        """自动重连（指数退避）"""
        if self._reconnect_attempts > 0:
            delay = min(2 ** self._reconnect_attempts, self._max_reconnect_delay)
            now = time.time()
            if now - self._last_reconnect_time < delay:
                return False  # 还在退避中

        self._reconnect_attempts += 1
        self._last_reconnect_time = time.time()

        try:
            self.disconnect()
        except Exception:
            pass

        logger.info(f"设备 {self.device_name} 尝试重连 (第{self._reconnect_attempts}次)")
        return self.connect()

    def disconnect(self):
        """断开Modbus连接"""
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass  # socket 可能已经坏了
            self.connected = False

    def read_holding_registers(self, address: int, count: int, 
                               slave_id: int | None = None) -> list[int] | None:
        """
        读取保持寄存器（功能码03）

        Args:
            address: 起始地址
            count: 读取数量
            slave_id: 从站地址（可选）

        Returns:
            list[int]: 寄存器值列表，失败返回None
        """
        if not self.connected:
            logger.error(f"设备 {self.device_name} 未连接")
            return None

        slave = slave_id or self.slave_id
        self._inc_stat('total_reads')

        try:
            result = self.client.read_holding_registers(
                address=address,
                count=count,
                slave=slave
            )

            if result.isError():
                logger.error(f"读取寄存器失败: {result}")
                self._inc_stat('failed_reads')
                with self._stats_lock:
                    self.stats['last_error'] = str(result)
                return None

            self._inc_stat('successful_reads')
            with self._stats_lock:
                self.stats['last_read_time'] = time.time()

            return result.registers

        except ConnectionException as e:
            logger.error(f"连接异常: {e}")
            self.connected = False
            self._inc_stat('failed_reads')
            with self._stats_lock:
                self.stats['last_error'] = str(e)
            # 自动重连
            self.reconnect()
            return None

        except Exception as e:
            logger.error(f"读取异常: {e}")
            self._inc_stat('failed_reads')
            with self._stats_lock:
                self.stats['last_error'] = str(e)
            return None

    def read_input_registers(self, address: int, count: int,
                             slave_id: int | None = None) -> list[int] | None:
        """
        读取输入寄存器（功能码04）

        Args:
            address: 起始地址
            count: 读取数量
            slave_id: 从站地址（可选）

        Returns:
            list[int]: 寄存器值列表，失败返回None
        """
        if not self.connected:
            logger.error(f"设备 {self.device_name} 未连接")
            return None

        slave = slave_id or self.slave_id
        self._inc_stat('total_reads')

        try:
            result = self.client.read_input_registers(
                address=address,
                count=count,
                slave=slave
            )

            if result.isError():
                logger.error(f"读取输入寄存器失败: {result}")
                self._inc_stat('failed_reads')
                return None

            self._inc_stat('successful_reads')
            with self._stats_lock:
                self.stats['last_read_time'] = time.time()

            return result.registers

        except ConnectionException as e:
            logger.error(f"连接异常: {e}")
            self.connected = False
            self._inc_stat('failed_reads')
            self.reconnect()
            return None

        except Exception as e:
            logger.error(f"读取异常: {e}")
            self._inc_stat('failed_reads')
            return None

    def read_coils(self, address: int, count: int,
                   slave_id: int | None = None) -> list[bool] | None:
        """
        读取线圈状态（功能码01）

        Args:
            address: 起始地址
            count: 读取数量
            slave_id: 从站地址（可选）

        Returns:
            list[bool]: 线圈状态列表，失败返回None
        """
        if not self.connected:
            return None

        slave = slave_id or self.slave_id

        try:
            result = self.client.read_coils(
                address=address,
                count=count,
                slave=slave
            )

            if result.isError():
                return None

            return result.bits[:count]

        except Exception as e:
            logger.error(f"读取线圈异常: {e}")
            return None

    def read_discrete_inputs(self, address: int, count: int,
                             slave_id: int | None = None) -> list[bool] | None:
        """
        读取离散输入（功能码02）

        Args:
            address: 起始地址
            count: 读取数量
            slave_id: 从站地址（可选）

        Returns:
            list[bool]: 离散输入状态列表，失败返回None
        """
        if not self.connected:
            return None

        slave = slave_id or self.slave_id

        try:
            result = self.client.read_discrete_inputs(
                address=address,
                count=count,
                slave=slave
            )

            if result.isError():
                return None

            return result.bits[:count]

        except Exception as e:
            logger.error(f"读取离散输入异常: {e}")
            return None

    def write_single_register(self, address: int, value: int,
                              slave_id: int | None = None) -> bool:
        """
        写入单个寄存器（功能码06）

        Args:
            address: 寄存器地址
            value: 写入值
            slave_id: 从站地址（可选）

        Returns:
            bool: 写入是否成功
        """
        if not self.connected:
            return False

        slave = slave_id or self.slave_id

        try:
            result = self.client.write_register(
                address=address,
                value=value,
                slave=slave
            )

            return not result.isError()

        except Exception as e:
            logger.error(f"写入寄存器异常: {e}")
            return False

    def write_single_coil(self, address: int, value: bool,
                          slave_id: int | None = None) -> bool:
        """
        写入单个线圈（功能码05）

        Args:
            address: 线圈地址
            value: 写入值
            slave_id: 从站地址（可选）

        Returns:
            bool: 写入是否成功
        """
        if not self.connected:
            return False

        slave = slave_id or self.slave_id

        try:
            result = self.client.write_coil(
                address=address,
                value=value,
                slave=slave
            )

            return not result.isError()

        except Exception as e:
            logger.error(f"写入线圈异常: {e}")
            return False

    def decode_float32(self, registers: list[int]) -> float | None:
        """
        解码32位浮点数（两个寄存器）

        Args:
            registers: 寄存器值列表（2个）

        Returns:
            float: 解码后的浮点数，NaN/Inf 返回 None
        """
        raw = (registers[0] << 16) | registers[1]
        value = struct.unpack('>f', struct.pack('>I', raw))[0]
        # 传感器断线时 PLC 返回 0xFFFF → NaN，过滤掉
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    def decode_float64(self, registers: list[int]) -> float | None:
        """解码64位浮点数（四个寄存器），NaN/Inf 返回 None"""
        raw = struct.pack('>HHHH', registers[0], registers[1], registers[2], registers[3])
        value = struct.unpack('>d', raw)[0]
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    def decode_uint16(self, register: int) -> int:
        """
        解码16位无符号整数

        Args:
            register: 寄存器值

        Returns:
            int: 解码后的整数
        """
        return register & 0xFFFF

    def decode_int16(self, register: int) -> int:
        """
        解码16位有符号整数

        Args:
            register: 寄存器值

        Returns:
            int: 解码后的整数
        """
        if register & 0x8000:
            return register - 0x10000
        return register

    def decode_int32(self, registers: list[int]) -> int:
        """
        解码32位有符号整数（两个寄存器，Big-Endian）

        Args:
            registers: 寄存器值列表（2个）

        Returns:
            int: 解码后的整数
        """
        raw = (registers[0] << 16) | registers[1]
        if raw & 0x80000000:
            raw -= 0x100000000
        return raw

    def decode_uint32(self, registers: list[int]) -> int:
        """
        解码32位无符号整数（两个寄存器，Big-Endian）

        Args:
            registers: 寄存器值列表（2个）

        Returns:
            int: 解码后的整数
        """
        return (registers[0] << 16) | registers[1]

    def get_stats(self) -> dict[str, Any]:
        """
        获取统计信息

        Returns:
            dict[str, Any]: 统计信息字典
        """
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            **self.stats
        }
