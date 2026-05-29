"""
三菱MC协议客户端 (Mitsubishi MC Protocol / SLMP Client)

实现三菱PLC的SLMP(Seamless Message Protocol)通信，支持：
- 3E帧（以太网TCP/UDP）
- 批量读取（字/位）
- 批量写入（字/位）
- 随机读写
- 设备型号：FX5U、Q系列、L系列、iQ-R系列

协议说明：
- 默认端口：5000
- 帧格式：3E帧（二进制格式）
- 支持设备：D(数据寄存器)、M(辅助继电器)、X(输入)、Y(输出)、
           R(文件寄存器)、W(链接寄存器)、B(链接继电器)、
           SD(特殊寄存器)、SM(特殊继电器)

依赖：无（纯socket实现）
"""

import struct
import socket
import logging
import threading
import time
from typing import Any

from .base_client import ModbusClientInterface

logger = logging.getLogger(__name__)

# 设备代码映射（SLMP协议标准）
DEVICE_CODES = {
    'D': 0xA8,    # 数据寄存器
    'R': 0xAF,    # 文件寄存器
    'W': 0xB4,    # 链接寄存器
    'SD': 0xA9,   # 特殊寄存器
    'M': 0x90,    # 辅助继电器
    'X': 0x9C,    # 输入
    'Y': 0x9D,    # 输出
    'B': 0xA0,    # 链接继电器
    'SM': 0x91,   # 特殊继电器
    'L': 0x92,    # 锁存继电器
    'F': 0x93,    # 报警器
    'V': 0x94,    # 边沿继电器
    'TS': 0xC1,   # 定时器触点
    'TC': 0xC0,   # 定时器线圈
    'TN': 0xC2,   # 定时器当前值
    'CS': 0xC4,   # 计数器触点
    'CC': 0xC3,   # 计数器线圈
    'CN': 0xC5,   # 计数器当前值
}

# 子命令码
SUBCMD_BIT_READ = 0x0001      # 位设备批量读取
SUBCMD_WORD_READ = 0x0000     # 字设备批量读取
SUBCMD_BIT_WRITE = 0x0003     # 位设备批量写入
SUBCMD_WORD_WRITE = 0x0002    # 字设备批量写入
SUBCMD_RANDOM_READ = 0x0006   # 随机读取
SUBCMD_RANDOM_WRITE = 0x0007  # 随机写入


class MCClient:
    """
    三菱MC协议客户端

    配置示例：
    {
        'id': 'fx5u_01',
        'name': '三菱FX5U PLC',
        'protocol': 'mc',
        'host': '192.168.1.56',
        'port': 5000,
        'network': 0,       # 网络号
        'pc': 0xFF,         # PC号 (0xFF=访问目标PLC)
        'timer': 10,        # 通信超时(秒)
    }
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.device_id = config.get('id', 'unknown')
        self.device_name = config.get('name', self.device_id)
        self.host = config.get('host', '192.168.1.56')
        self.port = config.get('port', 5000)
        self.network = config.get('network', 0)
        self.pc = config.get('pc', 0xFF)
        self.timer = config.get('timer', 10)

        self._sock = None
        self.connected = False
        self._lock = threading.Lock()

        self.stats = {
            'total_reads': 0,
            'successful_reads': 0,
            'failed_reads': 0,
            'total_writes': 0,
            'successful_writes': 0,
            'failed_writes': 0,
            'last_error': None,
        }

    def connect(self) -> bool:
        """建立TCP连接"""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.timer)
            self._sock.connect((self.host, self.port))
            self.connected = True
            logger.info(f"[MC] 设备 {self.device_name} 连接成功: {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"[MC] 设备 {self.device_name} 连接失败: {e}")
            self.stats['last_error'] = str(e)
            self.connected = False
            return False

    def disconnect(self):
        """断开连接"""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self.connected = False
        logger.info(f"[MC] 设备 {self.device_name} 已断开")

    def _build_3e_frame(self, command: int, subcommand: int, data: bytes) -> bytes:
        """
        构建3E帧

        帧格式：
        [副帧头(2)] [网络号(1)] [PC号(1)] [I/O编号(2)] [站号(1)]
        [请求数据长(2)] [监视定时器(2)] [命令(2)] [子命令(2)] [数据(N)]
        """
        # 请求数据长度 = 子命令(2) + 命令(2) + 数据
        data_len = 2 + 2 + len(data)

        # 副帧头
        sub_header = 0x5000  # 3E帧（二进制）

        frame = struct.pack('>H', sub_header)      # 副帧头
        frame += struct.pack('B', self.network)     # 网络号
        frame += struct.pack('B', self.pc)          # PC号
        frame += struct.pack('<H', 0x03FF)          # I/O编号 (固定0x03FF)
        frame += struct.pack('B', 0x00)             # 站号
        frame += struct.pack('<H', data_len)        # 请求数据长度
        frame += struct.pack('<H', self.timer * 2)  # 监视定时器 (单位50ms)
        frame += struct.pack('<H', command)         # 命令
        frame += struct.pack('<H', subcommand)      # 子命令
        frame += data                               # 数据

        return frame

    def _send_recv(self, frame: bytes) -> bytes | None:
        """发送请求帧并接收响应"""
        with self._lock:
            try:
                self._sock.send(frame)

                # 接收响应头（固定11字节）
                header = self._sock.recv(11)
                if len(header) < 11:
                    logger.error("[MC] 响应头不完整")
                    return None

                # 解析响应数据长度
                resp_data_len = struct.unpack('<H', header[9:11])[0]

                # 接收剩余数据
                resp_data = b''
                remaining = resp_data_len
                while remaining > 0:
                    chunk = self._sock.recv(remaining)
                    if not chunk:
                        break
                    resp_data += chunk
                    remaining -= len(chunk)

                # 检查完成码
                if len(resp_data) >= 2:
                    completion_code = struct.unpack('<H', resp_data[0:2])[0]
                    if completion_code != 0:
                        logger.error(f"[MC] 完成码错误: 0x{completion_code:04X}")
                        return None
                    return resp_data[2:]  # 返回去掉完成码的数据

                return resp_data

            except socket.timeout:
                logger.error("[MC] 通信超时")
                self.connected = False
                return None
            except Exception as e:
                logger.error(f"[MC] 通信异常: {e}")
                self.stats['last_error'] = str(e)
                return None

    def _parse_device_address(self, device: str, address: int) -> tuple[int, int]:
        """
        解析设备地址

        Args:
            device: 设备名（如 'D', 'M', 'X', 'Y', 'W', 'R'）
            address: 地址编号

        Returns:
            (device_code, address) 元组
        """
        device_upper = device.upper()
        if device_upper not in DEVICE_CODES:
            raise ValueError(f"不支持的设备类型: {device}，支持: {list(DEVICE_CODES.keys())}")
        return DEVICE_CODES[device_upper], address

    def read_words(self, device: str, address: int, count: int) -> list[int] | None:
        """
        批量读取字设备（命令 0x0401 / 子命令 0x0000）

        Args:
            device: 设备名（如 'D', 'W', 'R'）
            address: 起始地址
            count: 读取数量

        Returns:
            list[int]: 字值列表，失败返回None
        """
        if not self.connected:
            return None

        device_code, addr = self._parse_device_address(device, address)

        # 构建请求数据：[设备代码(1)] [地址(3)] [点数(2)]
        data = struct.pack('B', device_code)
        data += struct.pack('<I', addr)[0:3]  # 3字节地址（小端）
        data += struct.pack('<H', count)

        frame = self._build_3e_frame(0x0401, SUBCMD_WORD_READ, data)
        resp = self._send_recv(frame)

        if resp is None:
            self.stats['failed_reads'] += 1
            return None

        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1

        # 解析响应数据
        values = []
        for i in range(count):
            offset = i * 2
            if offset + 2 <= len(resp):
                val = struct.unpack('<H', resp[offset:offset + 2])[0]
                values.append(val)
            else:
                values.append(0)

        return values

    def write_words(self, device: str, address: int, values: list[int]) -> bool:
        """
        批量写入字设备（命令 0x1401 / 子命令 0x0000）

        Args:
            device: 设备名
            address: 起始地址
            values: 写入值列表

        Returns:
            bool: 是否成功
        """
        if not self.connected:
            return False

        device_code, addr = self._parse_device_address(device, address)
        count = len(values)

        # 构建请求数据
        data = struct.pack('B', device_code)
        data += struct.pack('<I', addr)[0:3]
        data += struct.pack('<H', count)
        for val in values:
            data += struct.pack('<H', val & 0xFFFF)

        frame = self._build_3e_frame(0x1401, SUBCMD_WORD_WRITE, data)
        resp = self._send_recv(frame)

        self.stats['total_writes'] += 1
        if resp is not None:
            self.stats['successful_writes'] += 1
            return True
        else:
            self.stats['failed_writes'] += 1
            return False

    def read_bits(self, device: str, address: int, count: int) -> list[bool] | None:
        """
        批量读取位设备（命令 0x0401 / 子命令 0x0001）

        Args:
            device: 设备名（如 'M', 'X', 'Y', 'B', 'SM'）
            address: 起始地址
            count: 读取数量

        Returns:
            list[bool]: 位值列表
        """
        if not self.connected:
            return None

        device_code, addr = self._parse_device_address(device, address)

        data = struct.pack('B', device_code)
        data += struct.pack('<I', addr)[0:3]
        data += struct.pack('<H', count)

        frame = self._build_3e_frame(0x0401, SUBCMD_BIT_READ, data)
        resp = self._send_recv(frame)

        if resp is None:
            self.stats['failed_reads'] += 1
            return None

        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1

        # 解析位数据（每字节包含2个位，低4位和高4位）
        values = []
        for i in range(count):
            byte_idx = i // 2
            if byte_idx < len(resp):
                byte_val = resp[byte_idx]
                if i % 2 == 0:
                    values.append((byte_val & 0x01) != 0)
                else:
                    values.append((byte_val & 0x10) != 0)
            else:
                values.append(False)

        return values

    def write_bits(self, device: str, address: int, values: list[bool]) -> bool:
        """
        批量写入位设备（命令 0x1401 / 子命令 0x0003）

        Args:
            device: 设备名
            address: 起始地址
            values: 写入值列表

        Returns:
            bool: 是否成功
        """
        if not self.connected:
            return False

        device_code, addr = self._parse_device_address(device, address)
        count = len(values)

        # 构建位数据（每字节2个位）
        data = struct.pack('B', device_code)
        data += struct.pack('<I', addr)[0:3]
        data += struct.pack('<H', count)

        # 打包位值
        for i in range(0, count, 2):
            byte_val = 0
            if i < count and values[i]:
                byte_val |= 0x01
            if i + 1 < count and values[i + 1]:
                byte_val |= 0x10
            data += struct.pack('B', byte_val)

        frame = self._build_3e_frame(0x1401, SUBCMD_BIT_WRITE, data)
        resp = self._send_recv(frame)

        self.stats['total_writes'] += 1
        if resp is not None:
            self.stats['successful_writes'] += 1
            return True
        else:
            self.stats['failed_writes'] += 1
            return False

    def read_single_word(self, device: str, address: int) -> int | None:
        """读取单个字"""
        result = self.read_words(device, address, 1)
        return result[0] if result else None

    def write_single_word(self, device: str, address: int, value: int) -> bool:
        """写入单个字"""
        return self.write_words(device, address, [value & 0xFFFF])

    def read_single_bit(self, device: str, address: int) -> bool | None:
        """读取单个位"""
        result = self.read_bits(device, address, 1)
        return result[0] if result else None

    def write_single_bit(self, device: str, address: int, value: bool) -> bool:
        """写入单个位"""
        return self.write_bits(device, address, [value])

    def read_float32(self, device: str, address: int) -> float | None:
        """读取32位浮点数（占用2个字，大端字序）"""
        result = self.read_words(device, address, 2)
        if result and len(result) >= 2:
            # 三菱PLC字序：低地址在前
            raw = struct.pack('<HH', result[0], result[1])
            return struct.unpack('<f', raw)[0]
        return None

    def read_int32(self, device: str, address: int) -> int | None:
        """读取32位整数"""
        result = self.read_words(device, address, 2)
        if result and len(result) >= 2:
            return (result[1] << 16) | result[0]
        return None

    def write_float32(self, device: str, address: int, value: float) -> bool:
        """写入32位浮点数"""
        raw = struct.pack('<f', value)
        words = struct.unpack('<HH', raw)
        return self.write_words(device, address, list(words))

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            **self.stats
        }
