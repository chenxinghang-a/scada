"""
西门子S7协议网关 (S7 Gateway)

独立的S7协议网关服务，负责：
1. 通过S7协议连接西门子PLC (S7-200/300/400/1200/1500)
2. 读取DB/输入/输出/标志位数据
3. 转换为统一物模型
4. 通过MQTT发布标准化数据

支持：
- S7 TCP (以太网)
- 自动重连
- 多设备并发采集

依赖：
- python-snap7 (pip install python-snap7)
"""

import time
import struct
import logging
import threading
from typing import Any
from datetime import datetime

from .base_gateway import BaseGateway
from .thing_model import (
    DeviceTelemetry, ThingModelConverter,
    ProtocolType, DataQuality
)

# 可选依赖: python-snap7
try:
    import snap7
    from snap7.util import (
        get_real, get_int, get_dint, get_word, get_byte, get_bool,
        set_real, set_int, set_dint, set_word, set_byte, set_bool
    )
    SNAP7_AVAILABLE = True
except ImportError:
    SNAP7_AVAILABLE = False
    logging.getLogger(__name__).debug(
        "python-snap7未安装，S7网关不可用。安装: pip install python-snap7"
    )

    # BUG-084：布尔位编解码**不需要** snap7（就是移位/掩码）。
    # 没有这两个兜底时，bool 分支会抛 `name 'set_bool' is not defined` ——
    # 被 except 吞成"S7写入失败"，看板上就是"这个位怎么都写不进去"。
    def get_bool(data, byte_index: int, bit_index: int) -> bool:
        return bool((data[byte_index] >> bit_index) & 0x01)

    def set_bool(data, byte_index: int, bit_index: int, value: bool) -> None:
        if value:
            data[byte_index] |= (1 << bit_index)
        else:
            data[byte_index] &= (~(1 << bit_index)) & 0xFF


class S7Gateway(BaseGateway):
    """
    西门子S7协议网关

    配置示例：
    {
        "gateway_id": "s7_gateway_01",
        "mqtt_broker": "localhost",
        "mqtt_port": 1883,
        "poll_interval": 5.0,
        "devices": [
            {
                "device_id": "S7_1200_001",
                "host": "192.168.1.200",
                "rack": 0,
                "slot": 1,
                "registers": [
                    {"name": "temperature", "db": 1, "offset": 0, "type": "real"},
                    {"name": "pressure", "db": 1, "offset": 4, "type": "real"},
                    {"name": "status", "db": 1, "offset": 8, "type": "int"},
                    {"name": "count", "db": 1, "offset": 10, "type": "dint"}
                ]
            }
        ]
    }
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)

        if not SNAP7_AVAILABLE:
            self.logger.error("python-snap7未安装，S7网关无法使用")

        # S7客户端缓存
        self._clients: dict[str, Any] = {}
        #: 每设备的布尔写入锁：读-改-写必须原子，否则两个 API 线程写
        #: 同一字节的不同位会互相覆盖（后写者把前者置的位清掉）。
        self._write_locks: dict[str, threading.Lock] = {}

        # 寄存器配置缓存
        self._register_configs: dict[str, list[dict[str, Any]]] = {}

        # 解析设备配置
        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if device_id:
                self._register_configs[device_id] = device_config.get('registers', [])

    def connect(self) -> bool:
        """连接所有S7设备"""
        if not SNAP7_AVAILABLE:
            self.logger.error("python-snap7未安装，无法连接S7设备")
            return False

        all_connected = True

        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if not device_id:
                continue

            try:
                client = self._create_client(device_config)
                if client:
                    self._clients[device_id] = client
                    self.connected_devices[device_id] = True
                    self.logger.info(f"S7设备 {device_id} 连接成功")
                else:
                    self.connected_devices[device_id] = False
                    self.logger.error(f"S7设备 {device_id} 连接失败")
                    all_connected = False
            except Exception as e:
                self.logger.error(f"S7设备 {device_id} 连接异常: {e}")
                self.connected_devices[device_id] = False
                all_connected = False

        return all_connected

    def disconnect(self):
        """断开所有S7设备"""
        # round 373：先摘链再遍历（理由同 modbus_gateway.disconnect）。
        clients, self._clients = self._clients, {}
        for device_id, client in clients.items():
            try:
                client.disconnect()
                destroy = getattr(client, 'destroy', None)
                if callable(destroy):
                    try:
                        destroy()
                    except Exception as e:
                        self.logger.debug("释放S7客户端失败: %s", e)
                self.logger.info(f"S7设备 {device_id} 已断开")
            except Exception as e:
                self.logger.error(f"S7设备 {device_id} 断开异常: {e}")

        self._clients.clear()
        self.connected_devices.clear()

    def _create_client(self, device_config: dict[str, Any]) -> Any:
        """创建S7客户端"""
        if not SNAP7_AVAILABLE:
            return None

        host = device_config.get('host', 'localhost')
        rack = device_config.get('rack', 0)
        slot = device_config.get('slot', 1)

        client = None
        try:
            client = snap7.client.Client()
            client.connect(host, rack, slot)

            if client.get_connected():
                self.logger.info(f"S7连接成功: {host} (rack={rack}, slot={slot})")
                return client
            self.logger.error(f"S7连接失败: {host}")

        except Exception as e:
            self.logger.error(f"S7连接异常: {host} - {e}")

        # 连接失败/异常：关闭已创建客户端，避免 snap7 原生 socket 泄漏
        if client is not None:
            try:
                client.disconnect()
            except Exception as e:
                self.logger.debug("清理S7客户端失败: %s", e)
        return None

    def read_device_data(self, device_id: str) -> dict[str, float] | None:
        """
        读取单个S7设备的数据

        Returns:
            dict[str, float]: {register_name: value}
        """
        client = self._clients.get(device_id)
        if not client:
            self.logger.error(f"S7设备 {device_id} 未连接")
            return None

        register_configs = self._register_configs.get(device_id, [])
        if not register_configs:
            self.logger.warning(f"S7设备 {device_id} 无寄存器配置")
            return None

        result = {}

        for reg_config in register_configs:
            name = reg_config.get('name')
            db_number = reg_config.get('db', 1)
            offset = reg_config.get('offset', 0)
            data_type = reg_config.get('type', 'real')

            # BUG-084：DBX 布尔量要按位读。原来 `float(data[0])` 把整个字节
            # 当值 —— 同字节其它位为 1 时读出 128.0，联锁判据完全错。
            bit_offset = reg_config.get('bit')
            if bit_offset is None:
                bit_offset = reg_config.get('bit_offset')
            if bit_offset is None:
                bit_offset = 0

            try:
                value = self._read_register(client, db_number, offset, data_type,
                                            bit_offset=bit_offset)
                if value is not None:
                    result[name] = value
                else:
                    self.logger.warning(f"S7设备 {device_id} 寄存器 {name} 读取失败")
                    self._note_read_failure(device_id, name, "读取返回空/错误响应")
            except Exception as e:
                self.logger.error(f"S7设备 {device_id} 寄存器 {name} 读取异常: {e}")
                self._note_read_failure(device_id, name, str(e))

        return result if result else None

    def _read_register(self, client, db_number: int, offset: int,
                       data_type: str, bit_offset: int = 0) -> float | None:
        """
        读取单个S7寄存器

        Args:
            client: S7客户端
            db_number: DB块号
            offset: 偏移地址
            data_type: 数据类型 (real, int, dint, word, byte)
        """
        try:
            # 根据数据类型确定读取长度
            type_sizes = {
                'real': 4,    # float32
                'int': 2,     # int16
                'dint': 4,    # int32
                'word': 2,    # uint16
                'byte': 1,    # uint8
                'bool': 1,    # bool
            }
            if data_type not in type_sizes:
                self.logger.error(f"不支持的S7数据类型: {data_type}")
                return None
            size = type_sizes[data_type]

            # 读取DB数据
            data = client.db_read(db_number, offset, size)

            # 解码
            if data_type == 'real':
                return get_real(data, 0)
            elif data_type == 'int':
                return float(get_int(data, 0))
            elif data_type == 'dint':
                return float(get_dint(data, 0))
            elif data_type == 'word':
                return float(get_word(data, 0))
            elif data_type == 'byte':
                return float(get_byte(data, 0))
            elif data_type == 'bool':
                return float(get_bool(data, 0, bit_offset))
            else:
                self.logger.error(f"不支持的S7数据类型: {data_type}")
                return None

        except Exception as e:
            self.logger.error(f"S7读取异常: db{db_number}.{offset} ({data_type}) - {e}")
            return None

    def convert_to_telemetry(self, device_id: str, raw_data: dict[str, float]) -> DeviceTelemetry:
        """将原始S7数据转换为统一物模型"""
        return ThingModelConverter.from_s7_data(
            device_id=device_id,
            data=raw_data,
            gateway_id=self.gateway_id
        )

    def reconnect_device(self, device_id: str) -> bool:
        """重连单个S7设备"""
        device_config = next(
            (d for d in self.devices_config if d.get('device_id') == device_id),
            None
        )

        if not device_config:
            return False

        # 关闭旧连接，先摘除：新建失败时不能把已断开的旧客户端留在缓存
        old_client = self._clients.pop(device_id, None)
        if old_client:
            try:
                old_client.disconnect()
            except Exception as e:
                # 重连前关闭旧连接失败：旧连接可能残留，但不影响新连接建立
                self.logger.warning("重连前关闭旧连接失败 device=%s: %s", device_id, e)

        # 创建新连接
        try:
            client = self._create_client(device_config)
            if client:
                self._clients[device_id] = client
                self.connected_devices[device_id] = True
                self.logger.info(f"S7设备 {device_id} 重连成功")
                return True
        except Exception as e:
            self.logger.error(f"S7设备 {device_id} 重连失败: {e}")

        self.connected_devices[device_id] = False
        return False

    def write_register(self, device_id: str, db_number: int, offset: int,
                       value: float, data_type: str = 'real',
                       bit_offset: int = 0) -> bool:
        """
        写入S7寄存器（用于设备控制）

        Args:
            device_id: 设备ID
            db_number: DB块号
            offset: 偏移地址
            value: 写入值
            data_type: 数据类型
        """
        client = self._clients.get(device_id)
        if not client:
            return False

        try:
            # 根据数据类型准备数据
            if data_type == 'real':
                data = bytearray(4)
                set_real(data, 0, value)
            elif data_type == 'int':
                data = bytearray(2)
                set_int(data, 0, int(value))
            elif data_type == 'dint':
                data = bytearray(4)
                set_dint(data, 0, int(value))
            elif data_type == 'word':
                data = bytearray(2)
                set_word(data, 0, int(value))
            elif data_type == 'byte':
                data = bytearray(1)
                set_byte(data, 0, int(value))
            elif data_type in ('bool', 'boolean'):
                # round 369：`_read_register` 支持 bool，`write_register` 却漏了
                # 这个分支 —— S7 DB 块的布尔量（启动位/阀门位）根本写不进去。
                #
                # BUG-084：**必须读-改-写**。构造全零 bytearray 再整字节 db_write，
                # 会把同字节另外 7 个布尔位一起清零 —— 写一个阀门位 =
                # 悄悄关掉同一字节里的 7 个设备。
                # round 381：db_read→db_write 之间必须持锁。两个线程同时写
                # 同一字节的不同位会各自读到旧字节，后写者覆盖前者。
                with self._write_locks.setdefault(device_id, threading.Lock()):
                    data = bytearray(client.db_read(db_number, offset, 1))
                    set_bool(data, 0, bit_offset, bool(value))
                    client.db_write(db_number, offset, data)
                return True
            else:
                self.logger.error(f"不支持的S7写入类型: {data_type}")
                return False

            client.db_write(db_number, offset, data)
            return True

        except Exception as e:
            self.logger.error(f"S7写入失败: {e}")
            return False


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    config = {
        "gateway_id": "s7_gateway_test",
        "mqtt_broker": "localhost",
        "mqtt_port": 1883,
        "poll_interval": 5.0,
        "devices": [
            {
                "device_id": "S7_1200_001",
                "host": "192.168.1.200",
                "rack": 0,
                "slot": 1,
                "registers": [
                    {"name": "temperature", "db": 1, "offset": 0, "type": "real"},
                    {"name": "pressure", "db": 1, "offset": 4, "type": "real"},
                    {"name": "status", "db": 1, "offset": 8, "type": "int"}
                ]
            }
        ]
    }

    gateway = S7Gateway(config)

    try:
        gateway.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在停止...")
    finally:
        gateway.stop()
