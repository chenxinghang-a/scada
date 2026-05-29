"""
OPC UA协议网关 (OPC UA Gateway)

独立的OPC UA协议网关服务，负责：
1. 通过OPC UA连接工业设备/PLC/服务器
2. 订阅节点数据变化
3. 转换为统一物模型
4. 通过MQTT发布标准化数据

支持：
- OPC UA TCP (opc.tcp://)
- 订阅模式（数据变化自动推送）
- 自动重连
- 多设备并发采集

依赖：
- asyncua (pip install asyncua)
"""

import time
import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from .base_gateway import BaseGateway
from .thing_model import (
    DeviceTelemetry, ThingModelConverter,
    ProtocolType, DataQuality
)

# 可选依赖: asyncua
try:
    from asyncua import Client as AsyncOPCUAClient
    from asyncua import ua
    ASYNCUA_AVAILABLE = True
except ImportError:
    ASYNCUA_AVAILABLE = False
    logging.getLogger(__name__).debug(
        "asyncua未安装，OPC UA网关不可用。安装: pip install asyncua"
    )


class OPCUAGateway(BaseGateway):
    """
    OPC UA协议网关

    配置示例：
    {
        "gateway_id": "opcua_gateway_01",
        "mqtt_broker": "localhost",
        "mqtt_port": 1883,
        "poll_interval": 5.0,
        "devices": [
            {
                "device_id": "OPCUA_Server_001",
                "endpoint": "opc.tcp://192.168.1.300:4840",
                "security_mode": "None",  # None, Sign, SignAndEncrypt
                "username": "",
                "password": "",
                "nodes": [
                    {"name": "temperature", "node_id": "ns=2;s=Temperature", "type": "double"},
                    {"name": "pressure", "node_id": "ns=2;s=Pressure", "type": "double"},
                    {"name": "status", "node_id": "ns=2;s=RunningStatus", "type": "int32"}
                ]
            }
        ]
    }
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)

        if not ASYNCUA_AVAILABLE:
            self.logger.error("asyncua未安装，OPC UA网关无法使用")

        # OPC UA客户端缓存
        self._clients: Dict[str, Any] = {}

        # 节点配置缓存
        self._node_configs: Dict[str, List[Dict[str, Any]]] = {}

        # 节点ID映射缓存
        self._node_map: Dict[str, Dict[str, Any]] = {}  # device_id -> {name: node_id}

        # 异步事件循环
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

        # 解析设备配置
        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if device_id:
                self._node_configs[device_id] = device_config.get('nodes', [])
                self._node_map[device_id] = {
                    node.get('name'): node.get('node_id')
                    for node in device_config.get('nodes', [])
                    if node.get('name') and node.get('node_id')
                }

    def _start_event_loop(self):
        """启动异步事件循环（在独立线程中运行）"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def connect(self) -> bool:
        """连接所有OPC UA设备"""
        if not ASYNCUA_AVAILABLE:
            self.logger.error("asyncua未安装，无法连接OPC UA设备")
            return False

        # 启动异步事件循环线程
        self._loop_thread = threading.Thread(target=self._start_event_loop, daemon=True)
        self._loop_thread.start()

        # 等待事件循环启动
        time.sleep(0.1)

        all_connected = True

        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if not device_id:
                continue

            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._connect_device(device_config),
                    self._loop
                )
                success = future.result(timeout=30)

                if success:
                    self.connected_devices[device_id] = True
                    self.logger.info(f"OPC UA设备 {device_id} 连接成功")
                else:
                    self.connected_devices[device_id] = False
                    self.logger.error(f"OPC UA设备 {device_id} 连接失败")
                    all_connected = False

            except Exception as e:
                self.logger.error(f"OPC UA设备 {device_id} 连接异常: {e}")
                self.connected_devices[device_id] = False
                all_connected = False

        return all_connected

    async def _connect_device(self, device_config: dict[str, Any]) -> bool:
        """异步连接单个OPC UA设备"""
        device_id = device_config.get('device_id')
        endpoint = device_config.get('endpoint', 'opc.tcp://localhost:4840')
        username = device_config.get('username', '')
        password = device_config.get('password', '')

        try:
            client = AsyncOPCUAClient(endpoint)

            # 设置认证
            if username:
                client.set_user(username)
            if password:
                client.set_password(password)

            await client.connect()
            self._clients[device_id] = client

            # 订阅节点（如果配置了订阅模式）
            nodes = device_config.get('nodes', [])
            if nodes and device_config.get('subscribe', False):
                await self._setup_subscription(device_id, client, nodes)

            return True

        except Exception as e:
            self.logger.error(f"OPC UA连接失败: {endpoint} - {e}")
            return False

    async def _setup_subscription(self, device_id: str, client: Any,
                                   nodes: List[Dict[str, Any]]):
        """设置OPC UA订阅"""
        try:
            subscription = await client.create_subscription(1000, self)

            for node_config in nodes:
                node_id = node_config.get('node_id')
                if node_id:
                    node = client.get_node(node_id)
                    await subscription.subscribe_data_change(node)
                    self.logger.debug(f"订阅节点: {node_id}")

        except Exception as e:
            self.logger.error(f"OPC UA订阅设置失败: {e}")

    def disconnect(self):
        """断开所有OPC UA设备"""
        for device_id, client in self._clients.items():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    client.disconnect(),
                    self._loop
                )
                future.result(timeout=10)
                self.logger.info(f"OPC UA设备 {device_id} 已断开")
            except Exception as e:
                self.logger.error(f"OPC UA设备 {device_id} 断开异常: {e}")

        self._clients.clear()
        self.connected_devices.clear()

        # 停止事件循环
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread:
                self._loop_thread.join(timeout=5)

    def read_device_data(self, device_id: str) -> dict[str, float] | None:
        """
        读取单个OPC UA设备的数据

        Returns:
            dict[str, float]: {node_name: value}
        """
        client = self._clients.get(device_id)
        if not client:
            self.logger.error(f"OPC UA设备 {device_id} 未连接")
            return None

        node_configs = self._node_configs.get(device_id, [])
        if not node_configs:
            self.logger.warning(f"OPC UA设备 {device_id} 无节点配置")
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._read_device_data_async(client, node_configs),
                self._loop
            )
            return future.result(timeout=30)

        except Exception as e:
            self.logger.error(f"OPC UA设备 {device_id} 读取异常: {e}")
            return None

    async def _read_device_data_async(self, client: Any,
                                       node_configs: List[Dict[str, Any]]) -> dict[str, float]:
        """异步读取OPC UA节点数据"""
        result = {}

        for node_config in node_configs:
            name = node_config.get('name')
            node_id = node_config.get('node_id')
            data_type = node_config.get('type', 'double')

            try:
                node = client.get_node(node_id)
                value = await node.read_value()

                # 类型转换
                if data_type in ('double', 'float'):
                    result[name] = float(value)
                elif data_type in ('int32', 'int16', 'uint32', 'uint16'):
                    result[name] = float(int(value))
                elif data_type == 'bool':
                    result[name] = 1.0 if value else 0.0
                else:
                    result[name] = float(value)

            except Exception as e:
                self.logger.warning(f"OPC UA节点 {node_id} 读取失败: {e}")

        return result

    def convert_to_telemetry(self, device_id: str, raw_data: dict[str, float]) -> DeviceTelemetry:
        """将原始OPC UA数据转换为统一物模型"""
        return ThingModelConverter.from_opcua_data(
            device_id=device_id,
            data=raw_data,
            gateway_id=self.gateway_id
        )

    def reconnect_device(self, device_id: str) -> bool:
        """重连单个OPC UA设备"""
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
                future = asyncio.run_coroutine_threadsafe(
                    old_client.disconnect(),
                    self._loop
                )
                future.result(timeout=10)
            except Exception:
                pass

        # 创建新连接
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._connect_device(device_config),
                self._loop
            )
            success = future.result(timeout=30)

            if success:
                self.connected_devices[device_id] = True
                self.logger.info(f"OPC UA设备 {device_id} 重连成功")
                return True
        except Exception as e:
            self.logger.error(f"OPC UA设备 {device_id} 重连失败: {e}")

        self.connected_devices[device_id] = False
        return False

    def write_node(self, device_id: str, node_id: str, value: Any,
                   data_type: str = 'double') -> bool:
        """
        写入OPC UA节点（用于设备控制）

        Args:
            device_id: 设备ID
            node_id: 节点ID
            value: 写入值
            data_type: 数据类型
        """
        client = self._clients.get(device_id)
        if not client:
            return False

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._write_node_async(client, node_id, value, data_type),
                self._loop
            )
            return future.result(timeout=10)

        except Exception as e:
            self.logger.error(f"OPC UA写入失败: {e}")
            return False

    async def _write_node_async(self, client: Any, node_id: str,
                                 value: Any, data_type: str) -> bool:
        """异步写入OPC UA节点"""
        try:
            node = client.get_node(node_id)

            # 根据类型准备数据值
            if data_type in ('double', 'float'):
                dv = ua.DataValue(ua.Variant(float(value), ua.VariantType.Double))
            elif data_type == 'int32':
                dv = ua.DataValue(ua.Variant(int(value), ua.VariantType.Int32))
            elif data_type == 'int16':
                dv = ua.DataValue(ua.Variant(int(value), ua.VariantType.Int16))
            elif data_type == 'bool':
                dv = ua.DataValue(ua.Variant(bool(value), ua.VariantType.Boolean))
            else:
                dv = ua.DataValue(ua.Variant(float(value), ua.VariantType.Double))

            await node.write_value(dv)
            return True

        except Exception as e:
            self.logger.error(f"OPC UA写入异常: {node_id} - {e}")
            return False

    # OPC UA订阅回调
    def datachange_notification(self, node, val, data):
        """数据变化回调（订阅模式）"""
        self.logger.debug(f"OPC UA数据变化: {node} = {val}")

    def event_notification(self, event):
        """事件回调"""
        self.logger.debug(f"OPC UA事件: {event}")

    def status_change_notification(self, status):
        """状态变化回调"""
        self.logger.debug(f"OPC UA状态变化: {status}")


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    config = {
        "gateway_id": "opcua_gateway_test",
        "mqtt_broker": "localhost",
        "mqtt_port": 1883,
        "poll_interval": 5.0,
        "devices": [
            {
                "device_id": "OPCUA_Server_001",
                "endpoint": "opc.tcp://192.168.1.300:4840",
                "nodes": [
                    {"name": "temperature", "node_id": "ns=2;s=Temperature", "type": "double"},
                    {"name": "pressure", "node_id": "ns=2;s=Pressure", "type": "double"},
                    {"name": "status", "node_id": "ns=2;s=RunningStatus", "type": "int32"}
                ]
            }
        ]
    }

    gateway = OPCUAGateway(config)

    try:
        gateway.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在停止...")
    finally:
        gateway.stop()
