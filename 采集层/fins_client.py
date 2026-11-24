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

from .base_client import ByteOrderCapableDecoder

logger = logging.getLogger(__name__)

# 内存区域代码 —— **必须分成字访问与位访问两张表**。
#
# FINS 命令 0x0101（MEMORY AREA READ）的区域码字节在两种访问方式下不是一个值：
# 同一张表同时被 read_words 和 read_bits 用，等于让读字函数发出位访问码，
# 从站要么返回错位数据、要么直接报协议错误。原表的 CIO/W/H/A/T/C 六项填的
# 全是位访问码，导致「读 CIO 区的字」在生产环境从来没成功过。
#
# 规范值（OMRON W227 / CS-CJ 系列手册 MEMORY AREA READ）：
#            字访问      位访问
#   CIO      0xB0        0x30
#   W(WR)    0xB1        0x31
#   H(HR)    0xB2        0x32
#   A(AR)    0xB3        0x33
#   D(DM)    0x02        0x02   ← D 区两种访问共用同一码，靠 bit 字段区分
#   EM0-15   0xA0-0xAF   0xA0-0xAF
#   T(TIM)   0x89        0x09
#   C(CNT)   0x8A        0x0A
WORD_AREA_CODES = {
    'D': 0x02,     # DM 区（数据存储器）
    **{f'E{i:X}': 0xA0 + i for i in range(16)},   # EM 区 Bank 0..F
    'CIO': 0xB0,   # CIO 区（I/O 区），字访问
    'W': 0xB1,     # W 区（内部辅助继电器），字访问
    'H': 0xB2,     # H 区（保持继电器），字访问
    'A': 0xB3,     # A 区（辅助存储器），字访问
    'T': 0x89,     # T 区（定时器当前值），字访问
    'C': 0x8A,     # C 区（计数器当前值），字访问
}

BIT_AREA_CODES = {
    'D': 0x02,
    **{f'E{i:X}': 0xA0 + i for i in range(16)},
    'CIO': 0x30,
    'W': 0x31,
    'H': 0x32,
    'A': 0x33,
    'T': 0x09,
    'C': 0x0A,
}

# 兼容旧引用（外部只做「键是否存在」的检查，语义上等同于支持的区域集合）。
# 新代码请直接引用上面两张表，不要再用这一个。
MEMORY_AREA_CODES = WORD_AREA_CODES

# 位/字区域标识
BIT_AREAS = {'CIO', 'W', 'H', 'A', 'T', 'C'}
WORD_AREAS = {'D', 'E0', 'E1', 'E2', 'E3', 'E4', 'E5', 'E6', 'E7',
              'E8', 'E9', 'EA', 'EB', 'EC', 'ED', 'EE', 'EF'}


class FINSClient(ByteOrderCapableDecoder):
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
        'byte_order': 'ABCD',  # 32/64位值字节序（默认ABCD）
        'word_area': 'D',      # 字区域（保持寄存器映射，默认D）
        'bit_area': 'CIO',     # 位区域（线圈映射，默认CIO）
    }

    暴露 Modbus 兼容面（read_holding_registers / read_coils / decode_*），
    使采集层 ``_collect_modbus`` 无需区分协议即可统一采集。
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
            request = struct.pack('>4sIIII', b'FINS', 12, 0, 0, 0)
            self._sock.sendall(request)

            # 接收响应
            response = b''
            while len(response) < 24:
                chunk = self._sock.recv(24 - len(response))
                if not chunk:
                    return False
                response += chunk

            # 解析响应
            magic, length, command, error_code, client_node, server_node = \
                struct.unpack('>4sIIIII', response)

            if magic != b'FINS' or length != 16 or command != 1:
                logger.error(
                    f"[FINS] 握手响应头异常: magic={magic!r}, length={length}, command={command}"
                )
                return False

            if error_code != 0:
                logger.error(f"[FINS] 握手错误码: {error_code}")
                return False

            if not (0 <= client_node <= 255 and 0 <= server_node <= 255):
                logger.error(
                    f"[FINS] 握手节点号越界: client={client_node}, server={server_node}"
                )
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
            except Exception as e:
                # 安全忽略：即将断开连接，socket 可能已损坏，关闭失败无副作用
                logger.debug(f"[FINS] 设备 {self.device_name} socket 关闭失败: {e}")
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
        tcp_header = struct.pack('>4sIII', b'FINS', 8 + len(fins_cmd), 2, 0)

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
        if area_upper not in WORD_AREA_CODES:
            logger.error(f"[FINS] 不支持的区域: {area}，支持: {list(WORD_AREA_CODES.keys())}")
            return None

        area_code = WORD_AREA_CODES[area_upper]

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

        # 短响应不能补 0 再报成功：那是“正常的 0”，比缺数据更危险。
        if len(resp) < count * 2:
            logger.error("[FINS] 读字响应长度不足: 请求 %d 字，实收 %d 字节",
                         count, len(resp))
            self.stats['failed_reads'] += 1
            return None

        self.stats['successful_reads'] += 1
        return [struct.unpack('>H', resp[i * 2:i * 2 + 2])[0]
                for i in range(count)]

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
        if area_upper not in WORD_AREA_CODES:
            return False

        area_code = WORD_AREA_CODES[area_upper]
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

    # ============================================================
    # Modbus 兼容面（供采集层 _collect_modbus 统一调用）
    # ============================================================
    def read_holding_registers(self, address: int, count: int,
                               slave_id: int | None = None) -> list[int] | None:
        """读取保持寄存器（映射到字区域，默认 D）。

        返回与 ``ModbusClient.read_holding_registers`` 一致的格式：
        ``list[int]``，失败返回 None。
        """
        if not self.connected:
            return None
        word_area = self.config.get('word_area', 'D')
        return self.read_words(word_area, address, count)

    def read_input_registers(self, address: int, count: int,
                             slave_id: int | None = None) -> list[int] | None:
        """读取输入寄存器（仍映射到字区域）。"""
        if not self.connected:
            return None
        word_area = self.config.get('word_area', 'D')
        return self.read_words(word_area, address, count)

    def read_coils(self, address: int, count: int,
                   slave_id: int | None = None) -> list[bool] | None:
        """读取线圈状态（映射到位区域，默认 CIO）。

        FINS 位读取需要起始位参数，兼容面固定从 bit=0 开始。
        """
        if not self.connected:
            return None
        bit_area = self.config.get('bit_area', 'CIO')
        return self.read_bits(bit_area, address, 0, count)

    def read_discrete_inputs(self, address: int, count: int,
                             slave_id: int | None = None) -> list[bool] | None:
        """读取离散输入（映射到位区域）。"""
        if not self.connected:
            return None
        bit_area = self.config.get('bit_area', 'CIO')
        return self.read_bits(bit_area, address, 0, count)

    # ---- 写兼容面（round 369）----
    #
    # `智能层/device_control` 的写入口全部按
    # `hasattr(client, 'write_single_register')` / `write_single_coil` 判断
    # 设备是否可写。FINS 原来只有私有的 `write_single_word` / `write_bits`，
    # 于是所有欧姆龙 PLC 在联锁动作与远程控制时 100% 报
    # 「该设备的客户端不支持寄存器/线圈写入」—— 安全联锁等于失效。
    def write_single_register(self, address: int, value: int,
                              slave_id: int | None = None,
                              verify: bool = False) -> bool:
        """写入单个保持寄存器（映射到字区域，默认 D）。"""
        if not self.connected:
            return False
        word_area = self.config.get('word_area', 'D')
        ok = self.write_single_word(word_area, address, int(value))
        if ok and verify:
            read_back = self.read_holding_registers(address, 1, slave_id=slave_id)
            if not read_back or int(read_back[0]) != (int(value) & 0xFFFF):
                logger.warning("FINS 寄存器回读验证失败: D%d 写=%s 回读=%s",
                               address, value, read_back)
                return False
        return ok

    def write_multiple_registers(self, address: int, values: list[int],
                                 slave_id: int | None = None,
                                 verify: bool = False) -> bool:
        """Modbus 兼容面：连续写多个保持寄存器（round 370）。

        缺这个方法时，调用方 `hasattr(client, 'write_multiple_registers')`
        恒为 True（基类有抽象实现），一调就抛 NotImplementedError ——
        浮点/32 位整数设定值全部写不进去。
        """
        if not self.connected or not values:
            return False
        word_area = self.config.get('word_area', 'D')
        words = [int(v) & 0xFFFF for v in values]
        ok = self.write_words(word_area, address, words)
        if ok and verify:
            read_back = self.read_holding_registers(
                address, len(words), slave_id=slave_id)
            if not read_back or [int(v) & 0xFFFF for v in read_back] != words:
                logger.warning("FINS 多寄存器回读验证失败: D%d 写=%s 回读=%s",
                               address, words, read_back)
                return False
        return ok

    def write_single_coil(self, address: int, value: bool,
                          slave_id: int | None = None,
                          verify: bool = False) -> bool:
        """写入单个线圈（映射到位区域，默认 CIO）。"""
        if not self.connected:
            return False
        bit_area = self.config.get('bit_area', 'CIO')
        ok = self.write_bits(bit_area, address, 0, [bool(value)])
        if ok and verify:
            read_back = self.read_coils(address, 1, slave_id=slave_id)
            if not read_back or bool(read_back[0]) != bool(value):
                logger.warning("FINS 线圈回读验证失败: CIO%d 写=%s 回读=%s",
                               address, value, read_back)
                return False
        return ok

    # decode_float32/uint16/int16/uint32/int32/float64 来自 ByteOrderCapableDecoder，
    # 已按 self.config['byte_order'] 正确解码四种字节序。

    def read_float32(self, area: str, address: int) -> float | None:
        """读取32位浮点数（2个字）。

        round 369：按设备配置的 ``byte_order`` 解码。原来硬编码 `>HH` +
        `>f`（等价 ABCD），CDAB/BADC/DCBA 设备读出来全是乱码。
        """
        result = self.read_words(area, address, 2)
        if result and len(result) >= 2:
            return self.decode_float32(result)
        return None

    def read_int32(self, area: str, address: int) -> int | None:
        """读取32位有符号整数（支持补码与四种字节序）。

        round 369：原来 `(result[0] << 16) | result[1]` 既不做符号扩展
        （-1 读成 +4294967295）也忽略 byte_order。
        """
        result = self.read_words(area, address, 2)
        if result and len(result) >= 2:
            return self.decode_int32(result)
        return None

    def write_float32(self, area: str, address: int, value: float) -> bool:
        """写入32位浮点数（按设备 byte_order 编码，与读取同源）。"""
        from .base_client import encode_float32_registers
        return self.write_words(
            area, address,
            encode_float32_registers(value, self._resolve_byte_order().value))

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
        if area_upper not in BIT_AREA_CODES:
            return None

        area_code = BIT_AREA_CODES[area_upper]

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

        # 短响应不能补 False 再报成功：联锁量会被误判为“未动作”。
        if len(resp) < count:
            logger.error("[FINS] 读位响应长度不足: 请求 %d 位，实收 %d 字节",
                         count, len(resp))
            self.stats['failed_reads'] += 1
            return None

        self.stats['successful_reads'] += 1
        return [resp[i] != 0 for i in range(count)]

    def write_bits(self, area: str, address: int, bit: int, values: list[bool]) -> bool:
        """写入位"""
        if not self.connected:
            return False

        area_upper = area.upper()
        if area_upper not in BIT_AREA_CODES:
            return False

        area_code = BIT_AREA_CODES[area_upper]
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
