"""
IEC 60870-5-104 (DL/T 634.5104) 协议网关
国标SCADA通信协议 - 遥测、遥信、遥控、遥调

实现BaseGateway接口，支持：
- IEC 104 TCP通信 (默认端口2404)
- APCI帧解析 (I/S/U帧)
- ASDU解析 — 遥测(M_ME_NC_1短浮点、M_ME_NA_1归一化)、遥信(M_SP_NA_1单点)
- 控制命令 — 遥控(C_SC_NA_1单命令)、遥调(C_SE_NC_1设定值)
- 总召唤 (C_IC_NA_1)
- 自动重连、回调支持
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

#: 模块级 logger —— 供 `@staticmethod`（如 `IEC104ASDU.build_command`）使用，
#: 那些方法没有 `self`，拿不到实例 logger。
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# IEC 104 常量定义
# ──────────────────────────────────────────────

class COT:
    """传送原因 (Cause of Transfer)"""
    PERIODIC = 3
    SPONTANEOUS = 1
    REQUEST = 5
    ACTIVATION = 6
    ACTIVATION_CON = 7


class TypeID:
    """类型标识"""
    # 监视方向
    M_SP_NA_1 = 1       # 单点信息 (遥信)
    M_DP_NA_1 = 3       # 双点信息
    M_ME_NA_1 = 9       # 测量值, 归一化 (遥测)
    M_ME_NB_1 = 11      # 测量值, 标度化
    M_ME_NC_1 = 13      # 测量值, 短浮点数 (遥测)
    M_IT_NA_1 = 15      # 累计量

    # 控制方向
    C_SC_NA_1 = 45      # 单命令 (遥控)
    C_DC_NA_1 = 46      # 双命令
    C_SE_NA_1 = 48      # 设定命令, 归一化 (遥调)
    C_SE_NC_1 = 50      # 设定命令, 短浮点数 (遥调)

    # 系统命令
    C_IC_NA_1 = 100     # 总召唤


class UFrame:
    """U 帧控制码（IEC 60870-5-104 §5.3）

    ⚠️ 位分配（**别凭直觉写**）：
    ``bit0, bit1 = 1, 1``（U 帧标志）；``bit2``=STARTDT act、``bit3``=STARTDT con、
    ``bit4``=STOPDT act、``bit5``=STOPDT con、``bit6``=TESTFR act、``bit7``=TESTFR con。

    历史缺陷（2026-10-13 修）：`connect()` 发的是 **0x04**、`disconnect()` 发的是 **0x08** ——
    这两个值的 bit0/bit1 都是 0，**不符合 U 帧标志**，对端（`parse_frame` 的
    ``control & 0x03`` 判据同理）会把它解析成 **I 帧**（还把 0x04 当成发送序号 2）。
    后果：**STARTDT 握手永远建不起来**，连接随后因超时被对端关闭 ——
    现场表现是「连上了又马上掉」，而日志里只有一句「连接成功」。
    """
    STARTDT_ACT = 0x07
    STARTDT_CON = 0x0B
    STOPDT_ACT = 0x13
    STOPDT_CON = 0x23
    TESTFR_ACT = 0x43
    TESTFR_CON = 0x83


# ──────────────────────────────────────────────
# APCI 帧解析 / 构建
# ──────────────────────────────────────────────

class IEC104APCI:
    """应用规约控制信息 (APCI) — IEC 104 帧格式"""
    START_BYTE = 0x68

    @staticmethod
    def build_i_frame(send_seq: int, recv_seq: int, asdu: bytes) -> bytes:
        """构建I帧 (信息传输)"""
        length = len(asdu) + 4
        header = struct.pack('<BBHH',
            IEC104APCI.START_BYTE, length,
            send_seq << 1,       # 发送序列号
            recv_seq << 1,       # 接收序列号
        )
        return header + asdu

    @staticmethod
    def build_s_frame(recv_seq: int) -> bytes:
        """构建S帧 (确认)"""
        return struct.pack('<BBHH',
            IEC104APCI.START_BYTE, 4,
            0x01,               # S帧标识
            recv_seq << 1
        )

    @staticmethod
    def build_u_frame(control: int) -> bytes:
        """构建U帧 (控制命令)"""
        return struct.pack('<BBHH',
            IEC104APCI.START_BYTE, 4,
            control, 0
        )

    @staticmethod
    def parse_frame(data: bytes) -> dict:
        """解析IEC 104帧"""
        if len(data) < 6:
            return {'type': 'invalid'}
        start, length = struct.unpack('<BB', data[:2])
        if start != IEC104APCI.START_BYTE:
            return {'type': 'invalid'}

        control = struct.unpack('<H', data[2:4])[0]
        if control & 0x03 == 0x03:       # U帧
            return {'type': 'U', 'control': control}
        elif control & 0x03 == 0x01:     # S帧
            recv_seq = struct.unpack('<H', data[4:6])[0] >> 1
            return {'type': 'S', 'recv_seq': recv_seq}
        else:                            # I帧
            send_seq = control >> 1
            recv_seq = struct.unpack('<H', data[4:6])[0] >> 1
            asdu = data[6:]
            return {'type': 'I', 'send_seq': send_seq, 'recv_seq': recv_seq, 'asdu': asdu}


# ──────────────────────────────────────────────
# ASDU 解析 / 构建
# ──────────────────────────────────────────────

class IEC104ASDU:
    """应用服务数据单元 (ASDU)"""

    @staticmethod
    def parse_asdu(data: bytes) -> dict:
        """解析ASDU"""
        if len(data) < 6:
            return {}
        type_id = data[0]
        num = data[1] & 0x7F
        cot = data[2]
        originator = data[3]
        common_addr = struct.unpack('<H', data[4:6])[0]

        info_objects: list[dict] = []
        offset = 6
        for _ in range(num):
            if offset >= len(data):
                break

            # 信息对象地址 (IOA) — 3字节
            if offset + 2 < len(data):
                ioa = data[offset] | (data[offset + 1] << 8)
            else:
                ioa = data[offset]
            offset += 2
            # 第三字节 (高位，通常为0)
            if offset < len(data):
                offset += 1

            if type_id == TypeID.M_ME_NC_1:        # 短浮点数
                if offset + 5 <= len(data):
                    value = struct.unpack('<f', data[offset:offset + 4])[0]
                    quality = data[offset + 4]
                    info_objects.append({'ioa': ioa, 'value': value, 'quality': quality})
                    offset += 5
                else:
                    break

            elif type_id == TypeID.M_SP_NA_1:      # 单点信息
                if offset < len(data):
                    value = data[offset] & 0x01
                    info_objects.append({'ioa': ioa, 'value': float(value), 'quality': 0})
                    offset += 1
                else:
                    break

            elif type_id == TypeID.M_ME_NA_1:      # 归一化值
                if offset + 3 <= len(data):
                    raw_val = struct.unpack('<h', data[offset:offset + 2])[0]
                    value = raw_val / 32767.0
                    quality = data[offset + 2]
                    info_objects.append({'ioa': ioa, 'value': value, 'quality': quality})
                    offset += 3
                else:
                    break

            elif type_id == TypeID.C_IC_NA_1:      # 总召唤确认
                info_objects.append({'ioa': ioa, 'value': 0, 'quality': 0})
                offset += 1

            else:
                break

        return {
            'type_id': type_id,
            'num': num,
            'cot': cot,
            'common_addr': common_addr,
            'info_objects': info_objects
        }

    @staticmethod
    def build_command(type_id: int, ioa: int, value: float,
                      qual: int = 0, common_addr: int = 1) -> bytes:
        """
        构建控制方向ASDU

        ASDU结构:
          TypeID(1) + VSQ(1) + COT(1) + Originator(1) + CommonAddr(2) + IOA(3) + Data

        Returns:
            bytes: ASDU 字节串；**类型不支持或越界时返回 b''**（调用方据此报错，
            不得发出空 ASDU）。

        ⚠️ 历史缺陷（2026-10-13 修）：type_id 越界（如 999）会先撞进
        ``struct.pack('B', ...)`` 抛 ``struct.error`` —— 与「返回 b''」的契约不符，
        且异常会穿过 `send_command` 冒到 API 层。现在先校验再打包。
        """
        if not isinstance(type_id, int) or not (0 <= type_id <= 0xFF):
            logger.error(f"非法 TypeID: {type_id!r}（必须是 0-255 的整数）")
            return b''
        if type_id not in (TypeID.C_SE_NC_1, TypeID.C_SC_NA_1, TypeID.C_IC_NA_1):
            logger.error(f"不支持的 TypeID: {type_id}")
            return b''
        if not (0 <= ioa <= 0xFFFFFF):
            logger.error(f"IOA 越界: {ioa}（3 字节，合法范围 0-16777215）")
            return b''

        ioa_lo = ioa & 0xFF
        ioa_mid = (ioa >> 8) & 0xFF
        ioa_hi = (ioa >> 16) & 0xFF
        header = struct.pack('<BBBBHBBB',
            type_id, 1, COT.ACTIVATION, 0, common_addr,
            ioa_lo, ioa_mid, ioa_hi)

        if type_id == TypeID.C_SE_NC_1:            # 设定命令-短浮点
            return header + struct.pack('<fB', value, qual)
        if type_id == TypeID.C_SC_NA_1:            # 单命令
            return header + struct.pack('<B', int(value) & 0x01)
        return header + struct.pack('<B', 0x14)    # 总召唤：QOI = 20 (全体)


# ──────────────────────────────────────────────
# IEC 104 客户端
# ──────────────────────────────────────────────

class IEC104Client:
    """
    IEC 104客户端 — 连接子站/RTU

    管理一个TCP连接上的104协议会话：
    - 维护收发序列号
    - 自动发送S帧确认
    - 解析收到的ASDU并缓存最新值
    """

    def __init__(self, host: str, port: int = 2404, common_addr: int = 1,
                 timeout: float = 10.0):
        self.host = host
        self.port = port
        self.common_addr = common_addr
        self.timeout = timeout
        self.logger = logging.getLogger(f"IEC104.{host}:{port}")

        self.socket: socket.socket | None = None
        self.connected = False
        self.send_seq = 0
        self.recv_seq = 0

        self._lock = threading.Lock()
        self._recv_buffer = b''
        self._last_data: dict[int, dict] = {}   # ioa -> {value, quality, type_id, time}
        self._callbacks: list = []
        self._running = False
        self._recv_thread: threading.Thread | None = None

    # ── 连接管理 ──────────────────────────────

    def connect(self) -> bool:
        """连接IEC 104服务器"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.host, self.port))
            self.connected = True
            self.send_seq = 0
            self.recv_seq = 0

            # 发送STARTDT激活（0x07 —— 见 UFrame 的位分配说明；历史上误写成 0x04）
            self._send_u_frame(UFrame.STARTDT_ACT)

            # 启动接收线程
            self._running = True
            self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._recv_thread.start()

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
                self._send_u_frame(UFrame.STOPDT_ACT)   # STOPDT（0x13，历史误写成 0x08）
            except Exception as e:
                # 发送停止帧失败通常因连接已断开，属预期内且无副作用
                self.logger.debug("发送STOPDT失败 %s:%s: %s", self.host, self.port, e)
            try:
                self.socket.close()
            except Exception as e:
                # 关闭已失效的 socket 可能抛错，属预期内且无副作用
                self.logger.debug("关闭socket失败 %s:%s: %s", self.host, self.port, e)
        self.connected = False
        self.logger.info("已断开连接")

    # ── 发送 ──────────────────────────────────

    def _send_u_frame(self, control: int):
        """发送U帧"""
        frame = IEC104APCI.build_u_frame(control)
        with self._lock:
            if self.socket:
                self.socket.send(frame)

    def _send_s_frame(self):
        """发送S帧确认"""
        frame = IEC104APCI.build_s_frame(self.recv_seq)
        with self._lock:
            if self.socket:
                self.socket.send(frame)

    def _send_i_frame(self, asdu: bytes):
        """发送I帧"""
        frame = IEC104APCI.build_i_frame(self.send_seq, self.recv_seq, asdu)
        with self._lock:
            if self.socket:
                self.socket.send(frame)
                self.send_seq = (self.send_seq + 1) % 32768

    # ── 接收 ──────────────────────────────────

    def _recv_loop(self):
        """接收线程"""
        while self._running:
            try:
                data = self.socket.recv(4096) if self.socket else b''
                if not data:
                    if self._running:
                        self.logger.warning("连接被对端关闭")
                    break
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
        """处理接收缓冲区 — 帧分割"""
        while len(self._recv_buffer) >= 6:
            if self._recv_buffer[0] != 0x68:
                # 丢弃直到找到起始字节
                idx = self._recv_buffer.find(0x68)
                if idx < 0:
                    self._recv_buffer = b''
                    break
                self._recv_buffer = self._recv_buffer[idx:]
                continue

            length = self._recv_buffer[1]
            if len(self._recv_buffer) < length + 2:
                break   # 不完整帧，等待更多数据

            frame_data = self._recv_buffer[:length + 2]
            self._recv_buffer = self._recv_buffer[length + 2:]

            self._handle_frame(frame_data)

    def _handle_frame(self, frame_data: bytes):
        """处理单个完整帧"""
        parsed = IEC104APCI.parse_frame(frame_data)

        if parsed['type'] == 'U':
            ctrl = parsed['control']
            # U 帧控制位见 UFrame 的说明（bit2..bit7 依次为
            # STARTDT act/con、STOPDT act/con、TESTFR act/con）。
            # 历史实现用 0x10 判「STARTDT con」、0x40 判「TESTFR con」——两者都错位：
            # 0x10 是 STOPDT act、0x40 是 TESTFR act，日志会把现场调试带偏。
            if ctrl & 0x08:
                self.logger.info("收到 STARTDT con")
            if ctrl & 0x20:
                self.logger.info("收到 STOPDT con")
            if ctrl & 0x80:
                self.logger.info("收到 TESTFR con")
            if ctrl == UFrame.TESTFR_ACT:
                # 标准要求：收到 TESTFR act **必须**回 TESTFR con，
                # 否则对端在测试超时后判定链路失效并断开。
                #
                # ⚠️ 必须用**相等**而不是 `ctrl & UFrame.TESTFR_ACT`：
                # 后者是把「值常量」当位掩码用 —— 0x43 本身含 bit0/bit1，
                # 于是 TESTFR_CON(0x83) & 0x43 == 0x03 非零，**con 也会被应答**，
                # 两端会无限对答。（这个错我第一版就犯了，被反向对照用例当场抓住。）
                self._send_u_frame(UFrame.TESTFR_CON)

        elif parsed['type'] == 'S':
            pass   # S帧仅确认，无需处理

        elif parsed['type'] == 'I':
            self.recv_seq = (self.recv_seq + 1) % 32768
            self._send_s_frame()
            self._handle_asdu(parsed['asdu'])

    def _handle_asdu(self, asdu_data: bytes):
        """处理ASDU数据"""
        parsed = IEC104ASDU.parse_asdu(asdu_data)
        if not parsed or not parsed.get('info_objects'):
            return

        for obj in parsed['info_objects']:
            ioa = obj['ioa']
            self._last_data[ioa] = {
                'value': obj['value'],
                'quality': obj.get('quality', 0),
                'type_id': parsed['type_id'],
                'time': time.time()
            }
            for cb in self._callbacks:
                try:
                    cb(ioa, obj['value'], parsed)
                except Exception as e:
                    self.logger.error(f"回调错误: {e}")

    # ── 公共接口 ──────────────────────────────

    def read_data(self) -> dict[int, dict]:
        """获取最新数据快照"""
        return dict(self._last_data)

    def send_command(self, ioa: int, value: float,
                     type_id: int = TypeID.C_SE_NC_1) -> bool:
        """发送控制/设定命令"""
        asdu = IEC104ASDU.build_command(type_id, ioa, value, common_addr=self.common_addr)
        if not asdu:
            return False
        self._send_i_frame(asdu)
        self.logger.info(f"发送命令: IOA={ioa}, 值={value}, TypeID={type_id}")
        return True

    def request_general_interrogation(self) -> bool:
        """发送总召唤命令"""
        asdu = IEC104ASDU.build_command(TypeID.C_IC_NA_1, 0, 0, common_addr=self.common_addr)
        if not asdu:
            return False
        self._send_i_frame(asdu)
        self.logger.info(f"发送总召唤: common_addr={self.common_addr}")
        return True

    def add_callback(self, callback):
        """添加数据回调"""
        self._callbacks.append(callback)


# ──────────────────────────────────────────────
# IEC 104 网关
# ──────────────────────────────────────────────

class IEC104Gateway(BaseGateway):
    """
    IEC 60870-5-104 协议网关

    配置示例：
    {
        "gateway_id": "iec104_gateway_01",
        "mqtt_broker": "localhost",
        "mqtt_port": 1883,
        "poll_interval": 5.0,
        "devices": [
            {
                "device_id": "RTU_001",
                "host": "192.168.1.200",
                "port": 2404,
                "common_addr": 1,
                "registers": [
                    {"name": "voltage", "ioa": 100},
                    {"name": "current", "ioa": 101},
                    {"name": "breaker_status", "ioa": 200}
                ]
            }
        ]
    }
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)

        # IEC 104 客户端缓存
        self._clients: dict[str, IEC104Client] = {}

        # IOA映射: device_id -> {register_name: ioa}
        self._ioa_maps: dict[str, dict[str, int]] = {}

        # 反向映射: device_id -> {ioa: register_name}
        self._reverse_ioa_maps: dict[str, dict[int, str]] = {}

        # 解析设备配置
        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if not device_id:
                continue
            ioa_map: dict[str, int] = {}
            for reg in device_config.get('registers', []):
                name = reg.get('name', '')
                ioa = reg.get('ioa', reg.get('address', 0))
                if name:
                    ioa_map[name] = ioa
            self._ioa_maps[device_id] = ioa_map
            self._reverse_ioa_maps[device_id] = {v: k for k, v in ioa_map.items()}

    def connect(self) -> bool:
        """连接所有IEC 104设备"""
        all_connected = True

        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if not device_id:
                continue

            host = device_config.get('host', '127.0.0.1')
            port = device_config.get('port', 2404)
            common_addr = device_config.get('common_addr',
                                            device_config.get('station_address', 1))

            client = IEC104Client(host, port, common_addr)
            if client.connect():
                self._clients[device_id] = client
                self.connected_devices[device_id] = True

                # 发送总召唤
                client.request_general_interrogation()

                self.logger.info(f"设备 {device_id} 连接成功 ({host}:{port})")
            else:
                self.connected_devices[device_id] = False
                self.logger.error(f"设备 {device_id} 连接失败 ({host}:{port})")
                all_connected = False

        return all_connected

    def disconnect(self):
        """断开所有IEC 104设备"""
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

        reverse_map = self._reverse_ioa_maps.get(device_id, {})
        result: dict[str, float] = {}

        for ioa, info in data.items():
            register_name = reverse_map.get(ioa, f"ioa_{ioa}")
            result[register_name] = float(info['value'])

        return result if result else None

    def convert_to_telemetry(self, device_id: str,
                             raw_data: dict[str, float]) -> DeviceTelemetry:
        """将原始IEC 104数据转换为统一物模型"""
        return ThingModelConverter.from_modbus_registers(
            device_id=device_id,
            registers=raw_data,
            gateway_id=self.gateway_id
        )

    def write_register(self, device_id: str, ioa: int, value: float,
                       type_id: int = TypeID.C_SE_NC_1) -> bool:
        """
        发送控制/设定命令

        Args:
            device_id: 设备ID
            ioa: 信息对象地址
            value: 设定值
            type_id: 类型标识 (C_SE_NC_1=短浮点, C_SC_NA_1=单命令)
        """
        client = self._clients.get(device_id)
        if not client:
            self.logger.error(f"设备 {device_id} 未连接")
            return False
        return client.send_command(ioa, value, type_id)

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
        port = device_config.get('port', 2404)
        common_addr = device_config.get('common_addr',
                                        device_config.get('station_address', 1))

        try:
            client = IEC104Client(host, port, common_addr)
            if client.connect():
                self._clients[device_id] = client
                self.connected_devices[device_id] = True
                client.request_general_interrogation()
                self.logger.info(f"设备 {device_id} 重连成功")
                return True
        except Exception as e:
            self.logger.error(f"设备 {device_id} 重连失败: {e}")

        self.connected_devices[device_id] = False
        return False
