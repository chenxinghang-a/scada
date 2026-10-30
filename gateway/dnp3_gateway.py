"""
DNP3 (Distributed Network Protocol 3) 协议网关
用于电力/水务/燃气SCADA系统

DNP3广泛用于北美电力/水务/燃气公用事业的SCADA系统，是IEC 60870-5-104的替代方案。

实现BaseGateway接口，支持：
- DNP3数据链路层帧解析（起始字节0x0564）
- 应用层功能码：READ(0x01)、WRITE(0x02)、DIRECT_OPERATE(0x05)
- 对象组：BI(0x01遥信)、AI(0x1E遥测)、BO(0x0C遥控)、AO(0x29遥调)
- TCP通信（默认端口20000）
- 自动重连、多设备支持

⚠️ 现状与已知缺口（2026-10-13 核实，round 323 更新，**这是「简化实现」，不能对真实从站工作**）
------------------------------------------------------------------------------
本模块是教学/演示级的**简化** DNP3，**未**通过一致性测试。逐条列清，避免被误读成
「DNP3 已支持」。**链路层 CRC（第 1–3 条）已在 round 323 补齐并通过标准测试向量**，
但下面第 4–6 条（对象头限定符、取值位、限定符覆盖）仍不符合标准 ——
也就是说：**帧能被从站接受了，但对象内容仍可能解析错**。

0. ✅ **已修（2026-10-13）**：起始字节曾写成小端 `64 05`（标准是 `05 64`）→
   真实从站丢帧、本模块也认不出真实从站的帧；`LENGTH` 曾写成 `len(app)+8`
   （标准是 `len(app)+5`，不含 CRC）。两条都只影响链路层字段，已按标准改正。

1. ✅ **已修（round 323）**：发送侧 CRC 换成标准 **CRC-16/DNP**
   （多项式 0x3D65 反射形式 0xA6BC、初值 0、xorout 0xFFFF），
   并加了标准测试向量（`crc16_dnp(b'123456789') == 0xEA82`）。
   原来用的是 `sum(header) & 0xFFFF`（字节和）—— 真实从站会丢弃每一个帧。

2. ✅ **已修（round 323）**：补上**用户数据块 CRC**（每 16 字节跟一个 CRC），
   帧长与 `_frame_size` 一致。原来比标准短 2 字节（缺数据块 CRC），
   真实从站会当成残帧丢弃。

3. ✅ **已修（round 323）**：接收侧**真的校验** CRC —— 帧头 CRC（覆盖前 8 字节）
   与每个数据块 CRC 都算；任一不符 → `valid: False` + `crc_verified: False`，
   网关 `_handle_response` 直接丢弃并计数（`_invalid_frames`，按 100 条节流告警）。
   原来 CRC 解析出来却从不比对，线路噪声造成的位翻转会让坏帧带着
   `valid: True` 一路往上走。
   顺带修了应用层取数：原来 `data[12:]` 把数据块 CRC 也当成应用层字节，
   多块数据时对象解析整体错位；现在先剥 CRC 再解析。
   顺带补强 `parse_frame` 的契约：拒 `LENGTH < 5`（标准最小 5）。这是**第二道防线** ——
   真实数据流里 `_process_buffer` 切帧时已用 `_frame_size()` 拦掉并重同步，
   该分支只在直接调用 `parse_frame` 时生效，**不要误读成主防线**。

4. **对象头限定符与范围不匹配**：`build_read_request` 用限定符 ``0x06``
   （= all objects，**不带范围字段**），却又紧跟了 4 字节 start/stop。
   → 要么用 0x06 且去掉范围，要么用 0x01（16 位 start/stop）。

5. **二进制输入的取值位与 variation 无关**：`parse_response` 用
   ``data[offset] & 0x01``。DNP3 Group 1 的 Var 1 是**打包位**（1 字节装 8 个点）、
   Var 2 的**状态位是 bit7**，而本函数不看 variation。

6. **`parse_response` 只处理 ``qualifier == 0x00``**，其余限定符直接返回空列表。

已修（2026-10-13）：**接收侧原本没有帧重组** —— `_recv_loop` 把每次 `recv()` 的
一包直接丢给 `parse_frame`，于是「粘包」时丢掉同一包里的后续帧、
「半包」时被判 `Frame too short` 静默丢弃（TCP 是字节流，这两种都必然发生）。
现在有 `_recv_buffer` + `_process_buffer()` 按标准帧长切帧。

要真正接入现场 DNP3 从站，第 1–5 条都要按 IEEE 1815 补齐，并用一致性测试向量验证。
在那之前，本模块只应在**模拟/演示**场景使用。
"""

import socket
import struct
import threading
import time
import logging
from typing import Any

from .base_gateway import BaseGateway
from .thing_model import (
    DeviceTelemetry, ThingModelConverter,
    ProtocolType, DataQuality
)


# ──────────────────────────────────────────────
# DNP3 常量定义
# ──────────────────────────────────────────────

class DNP3FunctionCode:
    """DNP3功能码"""
    READ = 0x01
    WRITE = 0x02
    SELECT = 0x03
    OPERATE = 0x04
    DIRECT_OPERATE = 0x05
    DIRECT_OPERATE_NR = 0x06
    COLD_RESTART = 0x0D
    WARM_RESTART = 0x0E
    ENABLE_UNSOLICITED = 0x14
    DISABLE_UNSOLICITED = 0x15
    RESPONSE = 0x81


class DNP3ObjectGroup:
    """DNP3对象组"""
    BI = 0x01      # 二进制输入（遥信）
    DBI = 0x02     # 双位二进制输入
    BO = 0x0C      # 二进制输出（遥控）
    AI = 0x1E      # 16位模拟输入（遥测）
    AI16 = 0x1E    # 16位模拟输入（AI的别名）
    AO = 0x29      # 模拟输出（遥调）
    CA = 0x3C      # 类别对象
    AI32 = 0x32    # 32位模拟输入


# ──────────────────────────────────────────────
# DNP3 帧解析 / 构建
# ──────────────────────────────────────────────

def crc16_dnp(data: bytes) -> int:
    """CRC-16/DNP（IEEE 1815 规定的链路层校验）。

    参数（别改，改了就与标准不符）：
      · 多项式 0x3D65，**反射**（reflected 形式 0xA6BC）
      · 初值 0x0000
      · 结果再与 0xFFFF 异或（xorout）

    校验值（标准测试向量）：``crc16_dnp(b'123456789') == 0xEA82``。
    本函数**逐位**实现而不是查表：DNP3 帧很短（≤292 字节），
    逐位实现慢不了多少，但少一张 512 字节的表、也少一处抄错的可能。
    """
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA6BC
            else:
                crc >>= 1
    return crc ^ 0xFFFF


def _split_data_blocks(payload: bytes) -> list[bytes]:
    """把用户数据按 16 字节切块（每块后面要跟一个 CRC）。"""
    return [payload[i:i + 16] for i in range(0, len(payload), 16)]


def build_dnp3_frame(control: int, destination: int, source: int,
                     app_data: bytes) -> bytes:
    """按 IEEE 1815 组一个完整的链路层帧（含**真实** CRC）。

    结构::

        起始(2) + LENGTH(1) + 控制(1) + 目的(2) + 源(2) + CRC(2)
        + [用户数据 16 字节 + CRC(2)] * N

    * 帧头 CRC 覆盖**前 8 字节**（起始~源地址）；
    * 每 16 字节用户数据后跟一个 CRC，覆盖**该块数据**；
    * ``LENGTH`` = 5 + 用户数据长度（**不含**任何 CRC）。

    ⚠️ 原实现用 ``sum(header) & 0xFFFF`` 当 CRC，且完全不加数据块 CRC ——
    真实从站会丢弃这样发出的每一个帧（见模块头注第 1、2 条）。
    """
    length = len(app_data) + 5
    header = DNP3Parser.START_BYTES + struct.pack('<BBHH',
                                                  length, control,
                                                  destination, source)
    out = bytearray(header)
    out += struct.pack('<H', crc16_dnp(header))
    for chunk in _split_data_blocks(app_data):
        out += chunk
        out += struct.pack('<H', crc16_dnp(chunk))
    return bytes(out)


class DNP3Parser:
    """DNP3协议解析器"""

    #: 起始字节：标准是 **0x05 0x64 这两个字节**（不是 0x0564 的小端形式）。
    #:
    #: ⚠️ 历史实现用 ``struct.pack('<H', 0x0564)`` 写到线上变成 ``64 05`` ——
    #: **两个字节反了**。后果是双向的：真实从站因起始字节不合法直接丢帧，
    #: 本模块也认不出真实从站发来的帧（``parse_frame`` 同样按小端比对）。
    #: 已改为按标准字节序处理（2026-10-13）。
    START_BYTES = b'\x05\x64'

    @staticmethod
    def _frame_size(length_byte: int) -> int | None:
        """按 DNP3 数据链路层格式算出**整帧字节数**。

        格式（IEEE 1815 / DL/T 634.5101）：

            起始(2) + LENGTH(1) + 控制(1) + 目的(2) + 源(2) + CRC(2)
            + 用户数据(≤16 字节) + CRC(2) + [每再满 16 字节数据块，追加一段 CRC]

        ``LENGTH`` 从**控制字节**数到用户数据末尾、**不含任何 CRC**，
        所以 ``LENGTH = 5 + 用户数据长度``。

        ⚠️ 因此整帧长度**不是** ``LENGTH + 2``：还要加上「每 16 字节数据块一个」的
        CRC。按 ``LENGTH + 2`` 切帧会把第 2 帧的起始字节当成数据吃掉，
        表现为「第 1 帧正常、之后全部丢帧」（帧边界一旦错位就再也对不齐）。

        Returns:
            int: 整帧字节数；``LENGTH < 5``（装不下控制+地址字段）时返回 None
        """
        if length_byte < 5:
            return None
        user_data = length_byte - 5
        crc_blocks = (user_data + 15) // 16
        return 2 + 1 + length_byte + 2 + 2 * crc_blocks

    @staticmethod
    def parse_frame(data: bytes) -> dict:
        """解析DNP3数据链路层帧"""
        if len(data) < 10:
            return {'valid': False, 'error': 'Frame too short'}

        if data[0:2] != DNP3Parser.START_BYTES:
            return {'valid': False, 'error': 'Invalid start bytes'}

        length = data[2]
        control = data[3]
        destination = struct.unpack('<H', data[4:6])[0]
        source = struct.unpack('<H', data[6:8])[0]
        crc = struct.unpack('<H', data[8:10])[0]

        # ---- LENGTH 下界校验（round 323，**第二道防线**）----
        #
        # LENGTH 是「从控制字节数到用户数据末尾、不含任何 CRC」的字节数，
        # 标准最小值为 5（即用户数据为空）。
        #
        # ⚠️ 真实数据流里这道校验**走不到**：`_process_buffer` 切帧时已先用
        # `_frame_size()` 拦掉 LENGTH < 5 并丢弃起始字节重同步
        # （见 `test_invalid_length_is_resynced`）。
        # 放在这里是因为 `parse_frame` 是 public staticmethod，可能被直接调用；
        # 且将来切帧逻辑若改动，这层还能兜住。**不要误读成主防线。**
        if length < 5:
            return {
                'valid': False,
                'error': 'Invalid LENGTH (< 5)',
                'length': length,
                'control': control,
                'destination': destination,
                'source': source,
                'crc': crc,
                'crc_verified': False,
            }

        # ---- 帧头 CRC 校验（round 323）----
        #
        # 原实现把 CRC 解析出来却**从不比对**，于是线路噪声造成的位翻转
        # 不会被发现，损坏帧带着 'valid': True 一路往上走。
        # 现在按标准重算：CRC-16/DNP 覆盖前 8 字节。
        header_crc_ok = (crc16_dnp(data[0:8]) == crc)

        # ---- 用户数据块 CRC 校验（round 323）----
        #
        # 用户数据每 16 字节跟一个 CRC。必须**先剥掉这些 CRC** 再交给应用层，
        # 否则应用层拿到的字节流里混着校验字节（原来 `data[12:]` 就是这么错的）。
        user_data_len = max(0, length - 5)
        payload = bytearray()
        data_crc_ok = True
        offset = 10
        remaining = user_data_len
        while remaining > 0:
            take = min(16, remaining)
            chunk = data[offset:offset + take]
            if len(chunk) < take:
                data_crc_ok = False          # 帧被截断
                break
            crc_bytes = data[offset + take:offset + take + 2]
            if len(crc_bytes) < 2:
                data_crc_ok = False
                break
            if crc16_dnp(chunk) != struct.unpack('<H', crc_bytes)[0]:
                data_crc_ok = False
            payload += chunk
            offset += take + 2
            remaining -= take

        app_control = payload[0] if len(payload) > 0 else 0
        function_code = payload[1] if len(payload) > 1 else 0
        app_data = bytes(payload[2:])

        crc_verified = header_crc_ok and data_crc_ok
        if not crc_verified:
            # **校验失败必须拒绝这一帧**。带着 valid=True 往上走，
            # 等于把"线路噪声"当成"现场数据"。
            reason = ('header CRC mismatch' if not header_crc_ok
                      else 'data block CRC mismatch')
            return {
                'valid': False,
                'error': reason,
                'length': length,
                'control': control,
                'destination': destination,
                'source': source,
                'crc': crc,
                'crc_verified': False,
                'function_code': function_code,
                'app_control': app_control,
                'data': app_data,
            }

        return {
            'valid': True,
            'length': length,
            'control': control,
            'destination': destination,
            'source': source,
            # 帧头 CRC（data[8:10]）。round 323 起**真的校验**：
            # CRC-16/DNP 覆盖前 8 字节，不符即拒帧（见上方 header_crc_ok）。
            # 历史实现只解析不比对，等于帧头校验形同虚设。
            'crc': crc,
            'crc_verified': True,
            'function_code': function_code,
            'app_control': app_control,
            'data': app_data,
        }

    @staticmethod
    def build_read_request(source: int, destination: int,
                           object_group: int, variation: int = 0,
                           start: int = 0, stop: int = 0x7FFF) -> bytes:
        """构建DNP3读请求帧"""
        # 应用层
        app_control = 0xC0  # FIR + FIN
        function_code = DNP3FunctionCode.READ

        # 对象头: Group(1) + Variation(1) + Qualifier(1) + Range(4)
        obj_header = struct.pack('<BBBHH',
                                 object_group, variation,
                                 0x06,  # 索引前缀 + 限定符
                                 start, stop)

        app_data = struct.pack('BB', app_control, function_code) + obj_header

        # 数据链路层：LENGTH 与链路层头统一由 build_dnp3_frame 构造。
        #   LENGTH 从**控制字节**数到用户数据末尾、**不含任何 CRC** → 5 + 用户数据长度；
        #   历史实现写 +8（把起始字节与 CRC 也算进去）→ 帧长字段偏大 3。
        #   历史实现还在 pack 时多传了一个 `0`（格式串只有 5 个格式符却给了 6 个值），
        #   struct.pack 必抛 "pack expected 5 items for packing (got 6)"，
        #   而 request_data() 的 except 把它吞掉 —— DNP3 从未真正发出过报文。
        # round 323：改用标准 CRC-16/DNP + 数据块 CRC（见 `build_dnp3_frame`）。
        #   原来 `sum(header) & 0xFFFF` 是假 CRC，真实从站会丢弃每一个帧。
        control = 0xC0  # DIR + PRM
        return build_dnp3_frame(control, destination, source, app_data)

    @staticmethod
    def build_direct_operate(source: int, destination: int,
                             index: int, value: int) -> bytes:
        """构建DNP3直接操作命令帧"""
        app_control = 0xC0
        function_code = DNP3FunctionCode.DIRECT_OPERATE

        # 对象: BO(Group 0x0C), Variation 1, 索引前缀
        obj_data = struct.pack('<BBBHHB',
                               DNP3ObjectGroup.BO, 1, 0x06,
                               index, index, value & 0x01)

        app_data = struct.pack('BB', app_control, function_code) + obj_data

        # 同 build_read_request：链路层头与 LENGTH 由 build_dnp3_frame 统一构造
        #   （LENGTH = 5 + 用户数据长度，不含 CRC）。
        control = 0xC0
        return build_dnp3_frame(control, destination, source, app_data)

    @staticmethod
    def parse_response(data: bytes) -> list[dict]:
        """解析DNP3响应中的对象数据"""
        objects = []
        if len(data) < 3:
            return objects

        group = data[0]
        variation = data[1]
        qualifier = data[2]
        offset = 3

        if qualifier == 0x00:  # 无索引前缀，所有对象
            while offset < len(data):
                if group in (DNP3ObjectGroup.AI, DNP3ObjectGroup.AI16):
                    if offset + 3 <= len(data):
                        value = struct.unpack('<h', data[offset:offset + 2])[0]
                        quality = data[offset + 2]
                        objects.append({
                            'group': group,
                            'index': len(objects),
                            'value': value,
                            'quality': quality,
                        })
                        offset += 3
                    else:
                        break
                elif group == DNP3ObjectGroup.AI32:
                    if offset + 5 <= len(data):
                        value = struct.unpack('<i', data[offset:offset + 4])[0]
                        quality = data[offset + 4]
                        objects.append({
                            'group': group,
                            'index': len(objects),
                            'value': value,
                            'quality': quality,
                        })
                        offset += 5
                    else:
                        break
                elif group in (DNP3ObjectGroup.BI, DNP3ObjectGroup.BO):
                    if offset + 1 <= len(data):
                        value = data[offset] & 0x01
                        objects.append({
                            'group': group,
                            'index': len(objects),
                            'value': value,
                            'quality': 0,
                        })
                        offset += 1
                    else:
                        break
                else:
                    break

        return objects


# ──────────────────────────────────────────────
# DNP3 客户端
# ──────────────────────────────────────────────

class DNP3Client:
    """
    DNP3客户端 — 管理与DNP3从站/Outstation的TCP连接

    功能：
    - 建立TCP连接（默认端口20000）
    - 发送读请求获取数据
    - 接收并解析响应，缓存最新值
    - 发送控制命令（直接操作）
    """

    def __init__(self, host: str, port: int = 20000,
                 local_addr: int = 1, remote_addr: int = 2,
                 timeout: float = 10.0):
        self.host = host
        self.port = port
        self.local_addr = local_addr    # 主站地址
        self.remote_addr = remote_addr  # 从站地址
        self.timeout = timeout
        self.logger = logging.getLogger(f"DNP3.{host}:{port}")

        self.socket: socket.socket | None = None
        self.connected = False
        self._lock = threading.Lock()
        self._last_data: dict[int, dict] = {}  # index -> {value, quality, group, time}
        self._running = False
        self._recv_thread: threading.Thread | None = None
        # 接收缓冲区：TCP 是字节流，DNP3 帧会**跨包/粘包**，
        # 必须自己重组（见 _process_buffer 的说明）。
        self._recv_buffer = b''
        # 被丢弃的无效帧累计数（CRC 不符 / 长度非法）。现场「数据不更新」时，
        # 这个计数是区分「设备没发」与「帧全坏了」的唯一依据 —— 所以必须在
        # __init__ 里显式初始化，不能靠 getattr 兜底（那样读的人会拿到
        # AttributeError，或者读到 0 而误判「从没坏过」）。
        self._invalid_frames = 0

    # ── 连接管理 ──────────────────────────────

    def connect(self) -> bool:
        """连接DNP3设备"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.host, self.port))
            self.connected = True
            self._running = True

            # 启动接收线程
            self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._recv_thread.start()

            # 发送初始读请求
            self.request_data()

            self.logger.info(f"连接成功: {self.host}:{self.port}")
            return True
        except Exception as e:
            self.logger.error(f"连接失败: {e}")
            self.connected = False
            return False

    def disconnect(self):
        """断开连接"""
        self._running = False
        if self.socket:
            try:
                self.socket.close()
            except Exception as e:
                # 关闭已失效的 socket 可能抛错（对端已断开等），属预期内且无副作用
                self.logger.debug("关闭socket失败 %s:%s: %s", self.host, self.port, e)
        self.connected = False
        self.logger.info("已断开连接")

    # ── 数据请求 ──────────────────────────────

    def request_data(self):
        """请求数据（读所有模拟输入）"""
        with self._lock:
            try:
                frame = DNP3Parser.build_read_request(
                    self.local_addr, self.remote_addr,
                    DNP3ObjectGroup.AI, variation=1)
                if self.socket:
                    self.socket.send(frame)
            except Exception as e:
                self.logger.error(f"读请求失败: {e}")

    def send_control(self, index: int, value: int) -> bool:
        """发送控制命令（直接操作）"""
        with self._lock:
            try:
                frame = DNP3Parser.build_direct_operate(
                    self.local_addr, self.remote_addr,
                    index, value)
                if self.socket:
                    self.socket.send(frame)
                self.logger.info(f"发送控制命令: index={index}, value={value}")
                return True
            except Exception as e:
                self.logger.error(f"控制命令失败: {e}")
                return False

    # ── 接收处理 ──────────────────────────────

    def _recv_loop(self):
        """接收线程"""
        while self._running:
            try:
                data = self.socket.recv(4096) if self.socket else b''
                if not data:
                    if self._running:
                        self.logger.warning("连接被对端关闭")
                    break
                # ⚠️ 必须先进缓冲区再切帧：直接把这一包丢给 parse_frame 会
                # ① 丢掉粘在同一包里的后续帧；② 把跨包的半帧当成「Frame too short」静默丢弃。
                self._recv_buffer += data
                self._process_buffer()
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    self.logger.error("接收线程socket错误")
                break
            except Exception as e:
                if self._running:
                    self.logger.error(f"接收异常: {e}")
                break
        self.connected = False

    def _process_buffer(self):
        """处理接收缓冲区 — 帧分割（DNP3 帧可能跨 TCP 包或粘包）"""
        while len(self._recv_buffer) >= 10:
            if self._recv_buffer[:2] != DNP3Parser.START_BYTES:
                idx = self._recv_buffer.find(DNP3Parser.START_BYTES)
                if idx < 0:
                    # 保留最后 1 字节：它可能是被截断的起始字节 0x05
                    self._recv_buffer = self._recv_buffer[-1:]
                    break
                self._recv_buffer = self._recv_buffer[idx:]
                continue

            size = DNP3Parser._frame_size(self._recv_buffer[2])
            if size is None:
                # LENGTH 非法（< 5，装不下控制/地址字段）→ 丢掉这对起始字节，继续重同步
                self.logger.warning("DNP3 帧 LENGTH 非法(%d)，丢弃起始字节重新同步",
                                    self._recv_buffer[2])
                self._recv_buffer = self._recv_buffer[2:]
                continue

            if len(self._recv_buffer) < size:
                break   # 不完整帧，等待更多数据

            frame = self._recv_buffer[:size]
            self._recv_buffer = self._recv_buffer[size:]
            self._handle_response(frame)

    def _handle_response(self, data: bytes):
        """处理DNP3响应（round 323：CRC 不符的帧在这里被丢弃，并计数）"""
        parsed = DNP3Parser.parse_frame(data)
        if not parsed.get('valid'):
            # round 323：坏帧必须**丢弃**（不能当数据用），但也不能静默 ——
            # 现场看到"数据不更新"时，要能区分"设备没发"和"帧坏了"。
            # 噪声风暴下每帧打一条日志会淹掉别的信息，所以按 100 条节流。
            self._invalid_frames += 1
            n = self._invalid_frames
            if n == 1 or n % 100 == 0:
                self.logger.warning(
                    "丢弃无效 DNP3 帧（累计 %d 个）：%s",
                    n, parsed.get('error', 'unknown'))
            return

        fc = parsed.get('function_code', 0)
        # DNP3 outstation只发送RESPONSE(0x81)，不会发送READ(0x01)
        if fc == DNP3FunctionCode.RESPONSE:
            objects = DNP3Parser.parse_response(parsed.get('data', b''))
            for obj in objects:
                self._last_data[obj['index']] = {
                    'value': obj['value'],
                    'quality': obj['quality'],
                    'group': obj['group'],
                    'time': time.time(),
                }

    # ── 公共接口 ──────────────────────────────

    def read_data(self) -> dict[int, dict]:
        """获取最新数据快照"""
        return dict(self._last_data)


# ──────────────────────────────────────────────
# DNP3 网关
# ──────────────────────────────────────────────

class DNP3Gateway(BaseGateway):
    """
    DNP3协议网关

    配置示例：
    {
        "gateway_id": "dnp3_gateway_01",
        "mqtt_broker": "localhost",
        "mqtt_port": 1883,
        "poll_interval": 5.0,
        "devices": [
            {
                "device_id": "RTU_001",
                "host": "192.168.1.100",
                "port": 20000,
                "local_addr": 1,
                "remote_addr": 2,
                "registers": [
                    {"name": "voltage", "index": 0, "group": "ai"},
                    {"name": "current", "index": 1, "group": "ai"},
                    {"name": "breaker_status", "index": 0, "group": "bi"}
                ]
            }
        ]
    }
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)

        # DNP3客户端缓存
        self._clients: dict[str, DNP3Client] = {}

        # 索引映射: device_id -> {register_name: (group, index)}
        self._register_maps: dict[str, dict[str, tuple[int, int]]] = {}

        # 反向映射: device_id -> {(group, index): register_name}
        self._reverse_maps: dict[str, dict[tuple[int, int], str]] = {}

        # 解析设备配置
        group_map = {
            'ai': DNP3ObjectGroup.AI,
            'bi': DNP3ObjectGroup.BI,
            'bo': DNP3ObjectGroup.BO,
            'ao': DNP3ObjectGroup.AO,
        }

        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if not device_id:
                continue
            reg_map: dict[str, tuple[int, int]] = {}
            for reg in device_config.get('registers', []):
                name = reg.get('name', '')
                index = reg.get('index', 0)
                group_str = reg.get('group', 'ai').lower()
                group = group_map.get(group_str, DNP3ObjectGroup.AI)
                if name:
                    reg_map[name] = (group, index)
            self._register_maps[device_id] = reg_map
            self._reverse_maps[device_id] = {v: k for k, v in reg_map.items()}

    def connect(self) -> bool:
        """连接所有DNP3设备"""
        all_connected = True

        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if not device_id:
                continue

            host = device_config.get('host', '127.0.0.1')
            port = device_config.get('port', 20000)
            local_addr = device_config.get('local_addr', 1)
            remote_addr = device_config.get('remote_addr', 2)

            client = DNP3Client(host, port, local_addr, remote_addr)
            if client.connect():
                self._clients[device_id] = client
                self.connected_devices[device_id] = True
                self.logger.info(f"设备 {device_id} 连接成功 ({host}:{port})")
            else:
                self.connected_devices[device_id] = False
                self.logger.error(f"设备 {device_id} 连接失败 ({host}:{port})")
                all_connected = False

        return all_connected

    def disconnect(self):
        """断开所有DNP3设备"""
        for device_id, client in self._clients.items():
            try:
                client.disconnect()
                self.logger.info(f"设备 {device_id} 已断开")
            except Exception as e:
                self.logger.error(f"设备 {device_id} 断开异常: {e}")

        self._clients.clear()
        self.connected_devices.clear()

    def read_device_data(self, device_id: str) -> dict[str, float] | None:
        """
        读取设备数据

        Returns:
            dict[str, float]: {register_name: value}
            None: 读取失败
        """
        client = self._clients.get(device_id)
        if not client or not client.connected:
            self.logger.error(f"设备 {device_id} 未连接")
            # round 295：设备级读取失败也要记账，否则 /stats 上 errors=0
            self._note_read_failure(device_id, "*", "设备未连接")
            return None

        data = client.read_data()
        if not data:
            self._note_read_failure(device_id, "*", "client.read_data() 返回空")
            return None

        reverse_map = self._reverse_maps.get(device_id, {})
        result: dict[str, float] = {}

        for index, info in data.items():
            group = info.get('group', DNP3ObjectGroup.AI)
            register_name = reverse_map.get((group, index), f"obj_{index}")
            result[register_name] = float(info['value'])

        return result if result else None

    def convert_to_telemetry(self, device_id: str,
                             raw_data: dict[str, float]) -> DeviceTelemetry:
        """将原始DNP3数据转换为统一物模型"""
        return ThingModelConverter.from_modbus_registers(
            device_id=device_id,
            registers=raw_data,
            gateway_id=self.gateway_id
        )

    def write_register(self, device_id: str, index: int, value: int) -> bool:
        """
        发送控制命令

        Args:
            device_id: 设备ID
            index: DNP3对象索引
            value: 控制值 (0/1)
        """
        client = self._clients.get(device_id)
        if not client:
            self.logger.error(f"设备 {device_id} 未连接")
            return False
        return client.send_control(index, value)

    def reconnect_device(self, device_id: str) -> bool:
        """重连单个设备"""
        device_config = next(
            (d for d in self.devices_config if d.get('device_id') == device_id),
            None
        )
        if not device_config:
            return False

        # 关闭旧连接
        old_client = self._clients.get(device_id)
        if old_client:
            try:
                old_client.disconnect()
            except Exception as e:
                # 重连前关闭旧连接失败：旧连接可能残留，但不影响新连接建立
                self.logger.debug("重连前关闭旧连接失败 device=%s: %s", device_id, e)

        # 创建新连接
        host = device_config.get('host', '127.0.0.1')
        port = device_config.get('port', 20000)
        local_addr = device_config.get('local_addr', 1)
        remote_addr = device_config.get('remote_addr', 2)

        try:
            client = DNP3Client(host, port, local_addr, remote_addr)
            if client.connect():
                self._clients[device_id] = client
                self.connected_devices[device_id] = True
                self.logger.info(f"设备 {device_id} 重连成功")
                return True
        except Exception as e:
            self.logger.error(f"设备 {device_id} 重连失败: {e}")

        self.connected_devices[device_id] = False
        return False
