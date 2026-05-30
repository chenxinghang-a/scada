"""
Modbus客户端模块
实现Modbus TCP/RTU协议通信
"""

import json
import math
import os
import time
import struct
import logging
import threading
from collections import deque
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from pymodbus.client import ModbusTcpClient, ModbusSerialClient
from pymodbus.exceptions import ModbusException, ConnectionException
from pymodbus.pdu import ExceptionResponse

logger = logging.getLogger(__name__)

# Modbus 异常码分类（Modbus Application Protocol V1.1b3）
# 永久错误：不重试，配置问题
_PERMANENT_EXCEPTIONS = {0x01, 0x02, 0x03}  # Illegal Function/Address/Value
# 瞬态错误：可重试
_TRANSIENT_EXCEPTIONS = {0x04, 0x06, 0x08, 0x0A, 0x0B}  # Device Failure/Busy/Parity/Gateway


class ByteOrder(Enum):
    """Modbus字节序（32/64位浮点数的寄存器排列方式）"""
    ABCD = 'ABCD'  # Big-endian（西门子S7-1200/1500默认）
    BADC = 'BADC'  # Big-endian，字交换（部分Allen-Bradley）
    CDAB = 'CDAB'  # Little-endian字序（西门子S7-300/400）
    DCBA = 'DCBA'  # Little-endian（部分三菱）


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
        self.byte_order = ByteOrder(config.get('byte_order', 'ABCD').upper())

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
        self._max_reconnect_delay = 60
        self._last_reconnect_time = 0
        self._consecutive_failures = 0  # 连续失败计数，超过阈值才重连

        # 通信日志（线程安全，保留最近 1000 条）
        self._log_lock = threading.Lock()
        self._comm_log: deque[dict[str, Any]] = deque(maxlen=1000)

        # 统计信息（线程安全）
        self._stats_lock = threading.Lock()
        self.stats: dict[str, Any] = {
            'total_reads': 0,
            'total_writes': 0,
            'successful_reads': 0,
            'failed_reads': 0,
            'successful_writes': 0,
            'failed_writes': 0,
            'last_read_time': None,
            'last_write_time': None,
            'last_error': None
        }

        # 统计持久化路径
        self._stats_dir = config.get(
            'stats_dir',
            str(Path(__file__).parent.parent / '数据存储' / 'modbus_stats')
        )

    def _inc_stat(self, key: str):
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    def _log_operation(self, operation: str, address: int, count: int,
                       success: bool, detail: str = ''):
        """记录一次读写操作到通信日志"""
        entry = {
            'timestamp': datetime.now().isoformat(timespec='milliseconds'),
            'operation': operation,
            'address': address,
            'count': count,
            'success': success,
            'detail': detail
        }
        with self._log_lock:
            self._comm_log.append(entry)

    def validate_address_range(self, address: int, count: int,
                               data_type: str = 'holding_register') -> bool:
        """
        GB/T 19582 寄存器地址范围校验

        Args:
            address: 起始地址 (0-65535)
            count:  读写数量 (1-125, Modbus PDU 限制)
            data_type: 数据类型
                - 'holding_register' / 'input_register': 寄存器类 (16-bit)
                - 'coil' / 'discrete_input': 位类 (bit)

        Returns:
            bool: 地址范围合法返回 True

        Raises:
            ValueError: 地址或数量超出 Modbus 规范范围
        """
        # 地址范围: 0x0000 - 0xFFFF
        if not (0 <= address <= 65535):
            raise ValueError(
                f"Modbus 地址越界: {address}，合法范围 0-65535 "
                f"(GB/T 19582 规范)"
            )

        # 数量范围: 1 - 125（Modbus PDU 最大 250 字节 / 2 = 125 个寄存器）
        # 线圈/离散输入的 PDU 限制也是 2000 bits，但实际实现通常限制 125 以统一处理
        if not (1 <= count <= 125):
            raise ValueError(
                f"Modbus 读写数量越界: {count}，合法范围 1-125 "
                f"(Modbus PDU 限制)"
            )

        # 溢出检查: address + count 不能超过 65536
        if address + count > 65536:
            raise ValueError(
                f"Modbus 地址溢出: 起始地址 {address} + 数量 {count} "
                f"= {address + count}，超出地址空间 65536"
            )

        return True

    def save_stats(self, file_path: str | None = None) -> str:
        """
        将通信统计信息持久化到 JSON 文件

        Args:
            file_path: 保存路径，None 则使用默认路径

        Returns:
            str: 实际保存的文件路径
        """
        if file_path is None:
            os.makedirs(self._stats_dir, exist_ok=True)
            safe_name = (self.device_name or self.device_id or 'unknown').replace('/', '_').replace('\\', '_')
            file_path = os.path.join(
                self._stats_dir,
                f"{safe_name}_stats.json"
            )

        with self._stats_lock:
            stats_copy = dict(self.stats)

        payload = {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'protocol': self.protocol,
            'saved_at': datetime.now().isoformat(timespec='milliseconds'),
            'stats': stats_copy
        }

        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        logger.debug(f"通信统计已保存: {file_path}")
        return file_path

    def get_communication_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """
        获取最近的通信操作日志

        Args:
            limit: 返回条数上限 (默认 100)

        Returns:
            list[dict]: 操作记录列表，每条含 timestamp/operation/address/count/success/detail
        """
        with self._log_lock:
            entries = list(self._comm_log)
        # 返回最新的 limit 条
        return entries[-limit:] if len(entries) > limit else entries

    def connect(self) -> bool:
        """建立Modbus连接"""
        try:
            if self.protocol == 'modbus_tcp':
                self.client = ModbusTcpClient(
                    host=self.host,
                    port=self.port,
                    timeout=5  # 5秒超时（3秒太激进，网络稍慢就断）
                )
            else:  # modbus_rtu
                # RTU 超时按 T3.5 规范：9600 波特率 ≈ 200ms，115200 ≈ 50ms
                rtu_timeout = max(0.05, min(0.5, 3.5 * 11 / self.baudrate * 10))
                self.client = ModbusSerialClient(
                    port=self.serial_port,
                    baudrate=self.baudrate,
                    parity=self.parity,
                    stopbits=self.stopbits,
                    bytesize=self.bytesize,
                    timeout=rtu_timeout
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
        success = self.connect()
        if success:
            self._consecutive_failures = 0
        return success

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
        # GB/T 19582 地址范围校验
        try:
            self.validate_address_range(address, count, 'holding_register')
        except ValueError as e:
            logger.error(str(e))
            self._log_operation('read_holding_registers', address, count, False, str(e))
            return None

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
                # 区分永久错误（配置问题）和瞬态错误（可重试）
                if isinstance(result, ExceptionResponse) and result.exception_code in _PERMANENT_EXCEPTIONS:
                    logger.error(f"Modbus 永久错误 (0x{result.exception_code:02X}): {result} — 不重试，检查寄存器配置")
                else:
                    logger.warning(f"Modbus 瞬态错误: {result} — 可重试")
                self._inc_stat('failed_reads')
                with self._stats_lock:
                    self.stats['last_error'] = str(result)
                self._log_operation('read_holding_registers', address, count, False, str(result))
                return None

            self._inc_stat('successful_reads')
            self._consecutive_failures = 0  # 成功读取，重置失败计数
            with self._stats_lock:
                self.stats['last_read_time'] = time.time()
            self._log_operation('read_holding_registers', address, count, True)

            return result.registers

        except ConnectionException as e:
            self._consecutive_failures += 1
            self._inc_stat('failed_reads')
            with self._stats_lock:
                self.stats['last_error'] = str(e)

            # 连续失败 3 次才判定断连并重连（避免单次超时就断）
            if self._consecutive_failures >= 3:
                logger.warning(f"设备 {self.device_name} 连续 {self._consecutive_failures} 次失败，触发重连")
                self.connected = False
                self.reconnect()
            else:
                logger.debug(f"设备 {self.device_name} 读取失败 ({self._consecutive_failures}/3): {e}")
            self._log_operation('read_holding_registers', address, count, False, str(e))
            return None

        except Exception as e:
            logger.error(f"读取异常: {e}")
            self._inc_stat('failed_reads')
            with self._stats_lock:
                self.stats['last_error'] = str(e)
            self._log_operation('read_holding_registers', address, count, False, str(e))
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
        # GB/T 19582 地址范围校验
        try:
            self.validate_address_range(address, count, 'input_register')
        except ValueError as e:
            logger.error(str(e))
            self._log_operation('read_input_registers', address, count, False, str(e))
            return None

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
                self._log_operation('read_input_registers', address, count, False, str(result))
                return None

            self._inc_stat('successful_reads')
            self._consecutive_failures = 0  # 成功读取，重置失败计数
            with self._stats_lock:
                self.stats['last_read_time'] = time.time()
            self._log_operation('read_input_registers', address, count, True)

            return result.registers

        except ConnectionException as e:
            self._consecutive_failures += 1
            self._inc_stat('failed_reads')
            with self._stats_lock:
                self.stats['last_error'] = str(e)
            if self._consecutive_failures >= 3:
                logger.warning(f"设备 {self.device_name} 连续 {self._consecutive_failures} 次失败，触发重连")
                self.connected = False
                self.reconnect()
            else:
                logger.debug(f"设备 {self.device_name} 读取失败 ({self._consecutive_failures}/3): {e}")
            self._log_operation('read_input_registers', address, count, False, str(e))
            return None

        except Exception as e:
            logger.error(f"读取异常: {e}")
            self._inc_stat('failed_reads')
            self._log_operation('read_input_registers', address, count, False, str(e))
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
        # GB/T 19582 地址范围校验
        try:
            self.validate_address_range(address, count, 'coil')
        except ValueError as e:
            logger.error(str(e))
            self._log_operation('read_coils', address, count, False, str(e))
            return None

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
                self._log_operation('read_coils', address, count, False, str(result))
                return None

            self._log_operation('read_coils', address, count, True)
            return result.bits[:count]

        except Exception as e:
            logger.error(f"读取线圈异常: {e}")
            self._log_operation('read_coils', address, count, False, str(e))
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
        # GB/T 19582 地址范围校验
        try:
            self.validate_address_range(address, count, 'discrete_input')
        except ValueError as e:
            logger.error(str(e))
            self._log_operation('read_discrete_inputs', address, count, False, str(e))
            return None

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
                self._log_operation('read_discrete_inputs', address, count, False, str(result))
                return None

            self._log_operation('read_discrete_inputs', address, count, True)
            return result.bits[:count]

        except Exception as e:
            logger.error(f"读取离散输入异常: {e}")
            self._log_operation('read_discrete_inputs', address, count, False, str(e))
            return None

    def write_single_register(self, address: int, value: int,
                              slave_id: int | None = None,
                              verify: bool = False) -> bool:
        """
        写入单个寄存器（功能码06），可选回读验证

        Args:
            address: 寄存器地址
            value: 写入值
            slave_id: 从站地址（可选）
            verify: 写后回读验证

        Returns:
            bool: 写入是否成功
        """
        # GB/T 19582 地址范围校验
        try:
            self.validate_address_range(address, 1, 'holding_register')
        except ValueError as e:
            logger.error(str(e))
            self._log_operation('write_single_register', address, 1, False, str(e))
            return False

        if not self.connected:
            return False

        slave = slave_id or self.slave_id
        self._inc_stat('total_writes')

        try:
            result = self.client.write_register(
                address=address,
                value=value,
                slave=slave
            )

            if isinstance(result, ExceptionResponse):
                logger.error(f"写入寄存器异常码 0x{result.exception_code:02X}")
                self._inc_stat('failed_writes')
                self._log_operation('write_single_register', address, 1, False, f"异常码 0x{result.exception_code:02X}")
                return False

            if result.isError():
                logger.error(f"写入寄存器失败: {result}")
                self._inc_stat('failed_writes')
                self._log_operation('write_single_register', address, 1, False, str(result))
                return False

            # 写后回读验证
            if verify:
                time.sleep(0.05)
                read_back = self.read_holding_registers(address, 1, slave_id=slave)
                if read_back and read_back[0] == value:
                    logger.debug(f"写入验证通过: addr={address}, value={value}")
                else:
                    logger.warning(f"写入验证失败: addr={address}, 写入={value}, 回读={read_back}")
                    self._inc_stat('failed_writes')
                    self._log_operation('write_single_register', address, 1, False, '回读验证失败')
                    return False

            self._inc_stat('successful_writes')
            with self._stats_lock:
                self.stats['last_write_time'] = time.time()
            self._log_operation('write_single_register', address, 1, True)
            return True

        except Exception as e:
            logger.error(f"写入寄存器异常: {e}")
            self._inc_stat('failed_writes')
            self._log_operation('write_single_register', address, 1, False, str(e))
            return False

    def write_single_coil(self, address: int, value: bool,
                          slave_id: int | None = None,
                          verify: bool = False) -> bool:
        """
        写入单个线圈（功能码05），可选回读验证

        Args:
            address: 线圈地址
            value: 写入值（True=ON, False=OFF）
            slave_id: 从站地址（可选）
            verify: 写后回读验证

        Returns:
            bool: 写入是否成功
        """
        # GB/T 19582 地址范围校验
        try:
            self.validate_address_range(address, 1, 'coil')
        except ValueError as e:
            logger.error(str(e))
            self._log_operation('write_single_coil', address, 1, False, str(e))
            return False

        if not self.connected:
            return False

        slave = slave_id or self.slave_id
        self._inc_stat('total_writes')

        try:
            result = self.client.write_coil(
                address=address,
                value=value,
                slave=slave
            )

            if isinstance(result, ExceptionResponse):
                logger.error(f"写入线圈异常码 0x{result.exception_code:02X}")
                self._inc_stat('failed_writes')
                self._log_operation('write_single_coil', address, 1, False, f"异常码 0x{result.exception_code:02X}")
                return False

            if result.isError():
                logger.error(f"写入线圈失败: {result}")
                self._inc_stat('failed_writes')
                self._log_operation('write_single_coil', address, 1, False, str(result))
                return False

            # 写后回读验证
            if verify:
                time.sleep(0.05)
                read_back = self.read_coils(address, 1, slave_id=slave)
                if read_back and read_back[0] == value:
                    logger.debug(f"线圈写入验证通过: addr={address}, value={value}")
                else:
                    logger.warning(f"线圈写入验证失败: addr={address}, 写入={value}, 回读={read_back}")
                    self._inc_stat('failed_writes')
                    self._log_operation('write_single_coil', address, 1, False, '回读验证失败')
                    return False

            self._inc_stat('successful_writes')
            with self._stats_lock:
                self.stats['last_write_time'] = time.time()
            self._log_operation('write_single_coil', address, 1, True)
            return True

        except Exception as e:
            logger.error(f"写入线圈异常: {e}")
            self._inc_stat('failed_writes')
            self._log_operation('write_single_coil', address, 1, False, str(e))
            return False

    def write_multiple_registers(self, address: int, values: list[int],
                                  slave_id: int | None = None) -> bool:
        """
        写入多个寄存器（功能码16，规范限制最多 123 个）

        Args:
            address: 起始地址
            values: 写入值列表
            slave_id: 从站地址（可选）

        Returns:
            bool: 写入是否成功
        """
        # GB/T 19582 地址范围校验
        count = len(values)
        try:
            self.validate_address_range(address, count, 'holding_register')
        except ValueError as e:
            logger.error(str(e))
            self._log_operation('write_multiple_registers', address, count, False, str(e))
            return False

        # FC16 (Write Multiple Registers) PDU 限制: 最多 123 个寄存器
        if count > 123:
            msg = f"批量写入寄存器数量 {count} 超出 PDU 限制 123"
            logger.error(msg)
            self._log_operation('write_multiple_registers', address, count, False, msg)
            return False

        if not self.connected:
            return False

        slave = slave_id or self.slave_id
        self._inc_stat('total_writes')

        try:
            result = self.client.write_registers(
                address=address,
                values=values,
                slave=slave
            )

            if isinstance(result, ExceptionResponse):
                logger.error(f"批量写入异常码 0x{result.exception_code:02X}")
                self._inc_stat('failed_writes')
                self._log_operation('write_multiple_registers', address, count, False, f"异常码 0x{result.exception_code:02X}")
                return False

            if result.isError():
                logger.error(f"批量写入失败: {result}")
                self._inc_stat('failed_writes')
                self._log_operation('write_multiple_registers', address, count, False, str(result))
                return False

            self._inc_stat('successful_writes')
            with self._stats_lock:
                self.stats['last_write_time'] = time.time()
            self._log_operation('write_multiple_registers', address, count, True)
            return True

        except Exception as e:
            logger.error(f"批量写入异常: {e}")
            self._inc_stat('failed_writes')
            self._log_operation('write_multiple_registers', address, count, False, str(e))
            return False

    def write_multiple_coils(self, address: int, values: list[bool],
                              slave_id: int | None = None) -> bool:
        """
        写入多个线圈（功能码15 / FC0F）

        Args:
            address: 起始地址
            values: 写入值列表 (True=ON, False=OFF)
            slave_id: 从站地址（可选）

        Returns:
            bool: 写入是否成功
        """
        # GB/T 19582 地址范围校验
        count = len(values)
        try:
            self.validate_address_range(address, count, 'coil')
        except ValueError as e:
            logger.error(str(e))
            self._log_operation('write_multiple_coils', address, count, False, str(e))
            return False

        if not self.connected:
            return False

        slave = slave_id or self.slave_id
        self._inc_stat('total_writes')

        try:
            result = self.client.write_coils(
                address=address,
                values=values,
                slave=slave
            )

            if isinstance(result, ExceptionResponse):
                logger.error(f"批量写入线圈异常码 0x{result.exception_code:02X}")
                self._inc_stat('failed_writes')
                self._log_operation('write_multiple_coils', address, count, False, f"异常码 0x{result.exception_code:02X}")
                return False

            if result.isError():
                logger.error(f"批量写入线圈失败: {result}")
                self._inc_stat('failed_writes')
                self._log_operation('write_multiple_coils', address, count, False, str(result))
                return False

            self._inc_stat('successful_writes')
            with self._stats_lock:
                self.stats['last_write_time'] = time.time()
            self._log_operation('write_multiple_coils', address, count, True)
            return True

        except Exception as e:
            logger.error(f"批量写入线圈异常: {e}")
            self._inc_stat('failed_writes')
            self._log_operation('write_multiple_coils', address, count, False, str(e))
            return False

    def read_write_multiple_registers(self, read_address: int, read_count: int,
                                       write_address: int, write_values: list[int],
                                       slave_id: int | None = None) -> list[int] | None:
        """
        读写多个寄存器（功能码23 / FC17）

        原子操作：先读取read_count个寄存器，再写入write_values。
        适用于需要"读-改-写"的场景。

        Args:
            read_address: 读取起始地址
            read_count: 读取数量
            write_address: 写入起始地址
            write_values: 写入值列表
            slave_id: 从站地址（可选）

        Returns:
            list[int]: 读取到的寄存器值，失败返回None
        """
        if not self.connected:
            return None

        slave = slave_id or self.slave_id

        try:
            result = self.client.readwrite_registers(
                read_address=read_address,
                read_count=read_count,
                write_address=write_address,
                write_registers=write_values,
                slave=slave
            )

            if isinstance(result, ExceptionResponse):
                logger.error(f"读写寄存器异常码 0x{result.exception_code:02X}")
                return None

            if result.isError():
                logger.error(f"读写寄存器失败: {result}")
                return None

            return result.registers

        except Exception as e:
            logger.error(f"读写寄存器异常: {e}")
            return None

    def decode_float32(self, registers: list[int]) -> float | None:
        """
        解码32位浮点数，支持4种字节序

        Args:
            registers: 寄存器值列表（2个）

        Returns:
            float: 解码后的浮点数，NaN/Inf 返回 None
        """
        if len(registers) < 2:
            raise ValueError("需要至少2个寄存器")

        w1, w2 = registers[0], registers[1]

        if self.byte_order == ByteOrder.ABCD:
            raw = (w1 << 16) | w2
        elif self.byte_order == ByteOrder.BADC:
            # Byte-swap within each word, then combine
            w1_swapped = ((w1 & 0xFF) << 8) | ((w1 >> 8) & 0xFF)
            w2_swapped = ((w2 & 0xFF) << 8) | ((w2 >> 8) & 0xFF)
            raw = (w1_swapped << 16) | w2_swapped
        elif self.byte_order == ByteOrder.CDAB:
            # Word swap only
            raw = (w2 << 16) | w1
        elif self.byte_order == ByteOrder.DCBA:
            b0 = (w1 >> 8) & 0xFF
            b1 = w1 & 0xFF
            b2 = (w2 >> 8) & 0xFF
            b3 = w2 & 0xFF
            raw = (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
        else:
            raw = (w1 << 16) | w2

        value = struct.unpack('!f', struct.pack('!I', raw))[0]
        # 传感器断线时 PLC 返回 0xFFFF → NaN，过滤掉
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    def decode_float64(self, registers: list[int]) -> float | None:
        """解码64位浮点数（四个寄存器），支持4种字节序，NaN/Inf 返回 None"""
        if len(registers) < 4:
            raise ValueError("需要至少4个寄存器")

        if self.byte_order == ByteOrder.ABCD:
            raw = struct.pack('>HHHH', registers[0], registers[1], registers[2], registers[3])
        elif self.byte_order == ByteOrder.BADC:
            raw = struct.pack('>HHHH', registers[1], registers[0], registers[3], registers[2])
        elif self.byte_order == ByteOrder.CDAB:
            raw = struct.pack('>HHHH', registers[2], registers[3], registers[0], registers[1])
        elif self.byte_order == ByteOrder.DCBA:
            # 每个寄存器内字节交换，寄存器顺序也反转
            def _swap16(w):
                return ((w & 0xFF) << 8) | ((w >> 8) & 0xFF)
            b0 = _swap16(registers[0])
            b1 = _swap16(registers[1])
            b2 = _swap16(registers[2])
            b3 = _swap16(registers[3])
            raw = struct.pack('>HHHH', b3, b2, b1, b0)
        else:
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
