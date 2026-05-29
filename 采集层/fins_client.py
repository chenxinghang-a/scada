"""
欧姆龙FINS协议客户端 (Omron FINS/TCP Client)

实现欧姆龙PLC的FINS(Factory Interface Network Service)通信，支持：
- FINS/TCP（以太网）
- 内存区域读写（DM/EM/CIO/W/H/D/T/C/AR/SR）
- 批量读取/写入
- 设备型号：NJ/NX/CJ/CP系列

协议说明：
- 默认端口：9600
- FINS/TCP握手：先发送FINS Node Address，再进行FINS通信
- 命令码：0x0101(读) / 0x0102(写)

依赖：无（纯socket实现）
"""

import struct
import socket
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# 内存区域代码
MEMORY_AREA_CODES = {
    'D': 0x02,     # DM区（数据存储器）
    'E0': 0x20,    # EM区（扩展数据存储器）Bank 0
    'E1': 0x21,    # EM区 Bank 1
    'E2': 0x22,    # EM区 Bank 2
    'E3': 0x23,    # EM区 Bank 3
    'E4': 0x24,    # EM区 Bank 4
    'E5': 0x25,    # EM区 Bank 5
    'E6': 0x26,    # EM区 Bank 6
    'E7': 0x27,    # EM区 Bank 7
    'E8': 0x28,    # EM区 Bank 8
    'E9': 0x29,    # EM区 Bank 9
    'EA': 0x2A,    # EM区 Bank A
    'EB': 0x2B,    # EM区 Bank B
    'EC': 0x2C,    # EM区 Bank C
    'ED': 0x2D,    # EM区 Bank D
    'EE': 0x2E,    # EM区 Bank E
    'EF': 0x2F,    # EM区 Bank F
    'CIO': 0x30,   # CIO区（I/O区）
    'W': 0x31,     # W区（内部辅助继电器）
    'H': 0x32,     # H区（保持继电器）
    'A': 0x33,     # A区（辅助存储器）
    'T': 0x09,     # T区（定时器当前值）
    'C': 0x0A,     # C区（计数器当前值）
}

# 位/字区域标识
BIT_AREAS = {'CIO', 'W', 'H', 'A', 'T', 'C'}
WORD_AREAS = {'D', 'E0', 'E1', 'E2', 'E3', 'E4', 'E5', 'E6', 'E7',
              'E8', 'E9', 'EA', 'EB', 'EC', 'ED', 'EE', 'EF'}


class FINSClient:
    """
    欧姆龙FINS/TCP客户端

    配置示例：
    {
        'id': 'nj501_01',
        'name': '欧姆龙NJ501 PLC',
        'protocol': 'fins',
        'host': '192.168.1.60',
        'port': 9600,
        'timeout': 10,
    }
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.device_id = config.get('id', 'unknown')
        self.device_name = config.get('name', self.device_id)
        self.host = config.get('host', '192.168.1.60')
        self.port = config.get('port', 9600)
        self.timeout = config.get('timeout', 10)

        self._sock = None
        self.connected = False
        self._lock = threading.Lock()
        self._sid = 0  # 服务ID（每次请求递增）

        # FINS节点地址
        self._local_node = 0
        self._remote_node = 0

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
        """建立FINS/TCP连接"""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout)
            self._sock.connect((self.host, self.port))

            # FINS/TCP握手
            if not self._fins_handshake():
                logger.error(f"[FINS] 握手失败: {self.host}:{self.port}")
                self.disconnect()
                return False

            self.connected = True
            logger.info(f"[FINS] 设备 {self.device_name} 连接成功: {self.host}:{self.port}")
            return True

        except Exception as e:
            logger.error(f"[FINS] 设备 {self.device_name} 连接失败: {e}")
            self.stats['last_error'] = str(e)
            self.connected = False
            return False

    def _fins_handshake(self) -> bool:
        """FINS/TCP握手（获取节点地址）"""
        try:
            # 发送FINS Node Address请求
            # [长度(4)] [命令(4)] [错误码(4)] [客户端节点(4)]
            request = struct.pack('>IIII', 8, 0, 0, 0)
            self._sock.send(request)

            # 接收响应
            response = self._sock.recv(24)
            if len(response) < 24:
                return False

            # 解析响应
            length, command, error_code, client_node, server_node = \
                struct.unpack('>IIIII', response[:20])

            if error_code != 0:
                logger.error(f"[FINS] 握手错误码: {error_code}")
                return False

            self._local_node = client_node
            self._remote_node = server_node
            return True

        except Exception as e:
            logger.error(f"[FINS] 握手异常: {e}")
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
        logger.info(f"[FINS] 设备 {self.device_name} 已断开")

    def _next_sid(self) -> int:
        """获取下一个服务ID"""
        self._sid = (self._sid + 1) % 256
        return self._sid

    def _build_fins_frame(self, command: int, data: bytes) -> bytes:
        """构建FINS/TCP帧"""
        # FINS/TCP头：[长度(4)] [命令(4)] [错误码(4)]
        # FINS命令：[ICF(1)] [RSV(1)] [GCT(1)] [DNA(1)] [DA1(1)] [DA2(1)]
        #           [SNA(1)] [SA1(1)] [SA2(1)] [SID(1)] [命令码(2)] [数据(N)]

        # FINS命令头
        fins_header = struct.pack('BBBBBBBBBB',
            0x80,               # ICF: 命令
            0x00,               # RSV: 保留
            0x02,               # GCT: 网关计数
            0x00,               # DNA: 目标网络号
            self._remote_node,  # DA1: 目标节点号
            0x00,               # DA2: 目标单元号
            0x00,               # SNA: 源网络号
            self._local_node,   # SA1: 源节点号
            0x00,               # SA2: 源单元号
            self._next_sid(),   # SID: 服务ID
        )

        # 完整FINS命令
        fins_cmd = fins_header + struct.pack('>H', command) + data

        # FINS/TCP帧
        tcp_header = struct.pack('>III', len(fins_cmd), 0, 0)

        return tcp_header + fins_cmd

    def _send_recv(self, frame: bytes) -> bytes | None:
        """发送FINS请求并接收响应"""
        with self._lock:
            try:
                self._sock.send(frame)

                # 接收FINS/TCP头（12字节）
                header = self._sock.recv(12)
                if len(header) < 12:
                    logger.error("[FINS] 响应头不完整")
                    return None

                length = struct.unpack('>I', header[0:4])[0]

                # 接收FINS数据
                fins_data = b''
                remaining = length
                while remaining > 0:
                    chunk = self._sock.recv(remaining)
                    if not chunk:
                        break
                    fins_data += chunk
                    remaining -= len(chunk)

                # 解析FINS响应
                # [ICF(1)] [RSV(1)] [GCT(1)] [DNA(1)] [DA1(1)] [DA2(1)]
                # [SNA(1)] [SA1(1)] [SA2(1)] [SID(1)] [命令码(2)] [完成码(2)] [数据(N)]
                if len(fins_data) < 14:
                    return None

                # 检查完成码
                completion_code = struct.unpack('>H', fins_data[12:14])[0]
                if completion_code != 0:
                    logger.error(f"[FINS] 完成码错误: 0x{completion_code:04X}")
                    return None

                return fins_data[14:]  # 返回去掉头和完成码的数据

            except socket.timeout:
                logger.error("[FINS] 通信超时")
                self.connected = False
                return None
            except Exception as e:
                logger.error(f"[FINS] 通信异常: {e}")
                self.stats['last_error'] = str(e)
                return None

    def read_words(self, area: str, address: int, count: int) -> list[int] | None:
        """
        批量读取字（命令 0x0101）

        Args:
            area: 内存区域（'D', 'W', 'H', 'CIO', 'E0'-'EF'等）
            address: 起始地址
            count: 读取数量

        Returns:
            list[int]: 字值列表
        """
        if not self.connected:
            return None

        area_upper = area.upper()
        if area_upper not in MEMORY_AREA_CODES:
            logger.error(f"[FINS] 不支持的区域: {area}，支持: {list(MEMORY_AREA_CODES.keys())}")
            return None

        area_code = MEMORY_AREA_CODES[area_upper]

        # 命令数据：[内存区代码(1)] [起始地址(2)] [起始位(1)] [读取数量(2)]
        data = struct.pack('B', area_code)
        data += struct.pack('>H', address)
        data += struct.pack('B', 0x00)  # 字模式，位地址=0
        data += struct.pack('>H', count)

        frame = self._build_fins_frame(0x0101, data)
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
                val = struct.unpack('>H', resp[offset:offset + 2])[0]
                values.append(val)
            else:
                values.append(0)

        return values

    def write_words(self, area: str, address: int, values: list[int]) -> bool:
        """
        批量写入字（命令 0x0102）

        Args:
            area: 内存区域
            address: 起始地址
            values: 写入值列表

        Returns:
            bool: 是否成功
        """
        if not self.connected:
            return False

        area_upper = area.upper()
        if area_upper not in MEMORY_AREA_CODES:
            return False

        area_code = MEMORY_AREA_CODES[area_upper]
        count = len(values)

        data = struct.pack('B', area_code)
        data += struct.pack('>H', address)
        data += struct.pack('B', 0x00)
        data += struct.pack('>H', count)
        for val in values:
            data += struct.pack('>H', val & 0xFFFF)

        frame = self._build_fins_frame(0x0102, data)
        resp = self._send_recv(frame)

        self.stats['total_writes'] += 1
        if resp is not None:
            self.stats['successful_writes'] += 1
            return True
        else:
            self.stats['failed_writes'] += 1
            return False

    def read_single_word(self, area: str, address: int) -> int | None:
        """读取单个字"""
        result = self.read_words(area, address, 1)
        return result[0] if result else None

    def write_single_word(self, area: str, address: int, value: int) -> bool:
        """写入单个字"""
        return self.write_words(area, address, [value & 0xFFFF])

    def read_float32(self, area: str, address: int) -> float | None:
        """读取32位浮点数（2个字）"""
        result = self.read_words(area, address, 2)
        if result and len(result) >= 2:
            # 欧姆龙字序：高地址在前（Big-Endian word order）
            raw = struct.pack('>HH', result[0], result[1])
            return struct.unpack('>f', raw)[0]
        return None

    def read_int32(self, area: str, address: int) -> int | None:
        """读取32位整数"""
        result = self.read_words(area, address, 2)
        if result and len(result) >= 2:
            return (result[0] << 16) | result[1]
        return None

    def write_float32(self, area: str, address: int, value: float) -> bool:
        """写入32位浮点数"""
        raw = struct.pack('>f', value)
        words = struct.unpack('>HH', raw)
        return self.write_words(area, address, list(words))

    def read_bits(self, area: str, address: int, bit: int, count: int) -> list[bool] | None:
        """
        读取位（命令 0x0101，位模式）

        Args:
            area: 内存区域（'CIO', 'W', 'H'等）
            address: 字地址
            bit: 起始位（0-15）
            count: 读取数量

        Returns:
            list[bool]: 位值列表
        """
        if not self.connected:
            return None

        area_upper = area.upper()
        if area_upper not in MEMORY_AREA_CODES:
            return None

        area_code = MEMORY_AREA_CODES[area_upper]

        data = struct.pack('B', area_code)
        data += struct.pack('>H', address)
        data += struct.pack('B', bit & 0x0F)
        data += struct.pack('>H', count)

        frame = self._build_fins_frame(0x0101, data)
        resp = self._send_recv(frame)

        if resp is None:
            self.stats['failed_reads'] += 1
            return None

        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1

        values = []
        for i in range(count):
            if i < len(resp):
                values.append(resp[i] != 0)
            else:
                values.append(False)

        return values

    def write_bits(self, area: str, address: int, bit: int, values: list[bool]) -> bool:
        """写入位"""
        if not self.connected:
            return False

        area_upper = area.upper()
        if area_upper not in MEMORY_AREA_CODES:
            return False

        area_code = MEMORY_AREA_CODES[area_upper]
        count = len(values)

        data = struct.pack('B', area_code)
        data += struct.pack('>H', address)
        data += struct.pack('B', bit & 0x0F)
        data += struct.pack('>H', count)
        for val in values:
            data += struct.pack('B', 0x01 if val else 0x00)

        frame = self._build_fins_frame(0x0102, data)
        resp = self._send_recv(frame)

        self.stats['total_writes'] += 1
        if resp is not None:
            self.stats['successful_writes'] += 1
            return True
        else:
            self.stats['failed_writes'] += 1
            return False

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            **self.stats
        }
