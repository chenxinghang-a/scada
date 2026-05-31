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

class DNP3Parser:
    """DNP3协议解析器"""

    START_BYTES = 0x0564

    @staticmethod
    def parse_frame(data: bytes) -> dict:
        """解析DNP3数据链路层帧"""
        if len(data) < 10:
            return {'valid': False, 'error': 'Frame too short'}

        start = struct.unpack('<H', data[0:2])[0]
        if start != DNP3Parser.START_BYTES:
            return {'valid': False, 'error': 'Invalid start bytes'}

        length = data[2]
        control = data[3]
        destination = struct.unpack('<H', data[4:6])[0]
        source = struct.unpack('<H', data[6:8])[0]
        crc = struct.unpack('<H', data[8:10])[0]

        # 解析应用层（如果存在）
        app_control = 0
        function_code = 0
        app_data = b''

        if len(data) > 10:
            app_control = data[10]
        if len(data) > 11:
            function_code = data[11]
        if len(data) > 12:
            app_data = data[12:]

        return {
            'valid': True,
            'length': length,
            'control': control,
            'destination': destination,
            'source': source,
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

        # 数据链路层
        control = 0xC0  # DIR + PRM
        length = len(app_data) + 8

        header = struct.pack('<HBBHH',
                             DNP3Parser.START_BYTES, length,
                             control, 0, destination, source)

        # 简化CRC（实际DNP3使用CRC-16/CCITT）
        crc = sum(header) & 0xFFFF

        return header + struct.pack('<H', crc) + app_data

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

        control = 0xC0
        length = len(app_data) + 8

        header = struct.pack('<HBBHH',
                             DNP3Parser.START_BYTES, length,
                             control, 0, destination, source)

        crc = sum(header) & 0xFFFF
        return header + struct.pack('<H', crc) + app_data

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
            except Exception:
                pass
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
                self._handle_response(data)
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

    def _handle_response(self, data: bytes):
        """处理DNP3响应"""
        parsed = DNP3Parser.parse_frame(data)
        if not parsed.get('valid'):
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
            return None

        data = client.read_data()
        if not data:
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
            except Exception:
                pass

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
