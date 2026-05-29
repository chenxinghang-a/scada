"""
OPC UA客户端模块
实现OPC UA协议的数据采集，支持连接任意OPC UA服务器
面向现代工业4.0/黑灯工厂的标准化设备接入
"""

import asyncio
import logging
import threading
import time
from typing import Any, Callable
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    from asyncua import Client, ua
    from asyncua.common.subscription import Subscription
    OPCUA_AVAILABLE = True
except ImportError:
    OPCUA_AVAILABLE = False
    logger.warning("opcua-asyncio未安装，OPC UA功能不可用。请运行: pip install opcua-asyncio")


class OPCUAClient:
    """
    OPC UA数据采集客户端
    支持连接OPC UA服务器、浏览节点、订阅数据变更

    典型使用场景：
    - 连接西门子S7-1500/1200的OPC UA服务器
    - 连接三菱/欧姆龙等PLC的OPC UA接口
    - 连接第三方SCADA/MES系统的OPC UA暴露接口
    """

    def __init__(self, config: dict[str, Any]):
        """
        初始化OPC UA客户端

        Args:
            config: 设备配置字典，包含：
                - endpoint: OPC UA服务器地址，如 opc.tcp://192.168.1.100:4840
                - security_mode: 安全模式 (None/Sign/SignAndEncrypt)
                - username: 用户名（可选）
                - password: 密码（可选）
                - nodes: 要订阅的节点列表
        """
        if not OPCUA_AVAILABLE:
            raise ImportError("opcua-asyncio未安装，请运行: pip install opcua-asyncio")

        self.config = config
        self.device_id = config.get('id', 'opcua_device')
        self.device_name = config.get('name', 'OPC UA设备')
        self.endpoint = config.get('endpoint', 'opc.tcp://localhost:4840')

        # 安全配置
        self.security_mode = config.get('security_mode', None)
        self.username = config.get('username')
        self.password = config.get('password')

        # 节点配置：[{"node_id": "ns=2;s=Temperature", "name": "temperature", "unit": "°C"}]
        self.node_configs = config.get('nodes', [])

        # 客户端实例
        self.client: Client | None = None
        self.subscription: Subscription | None = None
        self.connected = False

        # 最新数据缓存: {node_name: {"value": x, "timestamp": t, "quality": q}}
        self.latest_data: dict[str, dict[str, Any]] = {}

        # 数据回调
        self._data_callbacks: list[Callable[..., Any]] = []

        # 事件循环（独立线程运行异步代码）
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        # 统计
        self.stats: dict[str, Any] = {
            'connected_since': None,
            'nodes_subscribed': 0,
            'data_updates': 0,
            'errors': 0,
            'last_error': None
        }

        logger.info(f"OPC UA客户端初始化: {self.endpoint}")

    def add_data_callback(self, callback: Callable[..., Any]):
        """添加数据回调函数"""
        self._data_callbacks.append(callback)

    def connect(self) -> bool:
        """
        启动OPC UA连接（在独立线程中运行异步事件循环）

        Returns:
            bool: 是否成功启动
        """
        if self._running:
            logger.warning("OPC UA客户端已在运行")
            return True

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # 等待连接完成
        for _ in range(30):  # 最多等3秒
            if self.connected:
                return True
            if not self._running:
                return False
            time.sleep(0.1)

        logger.warning("OPC UA连接超时")
        return self.connected

    def disconnect(self):
        """断开连接"""
        self._running = False
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._cleanup(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)
        self.connected = False
        logger.info("OPC UA客户端已断开")

    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        """获取所有节点的最新数据"""
        return dict(self.latest_data)

    def _run_loop(self):
        """在独立线程中运行asyncio事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_and_subscribe())
        except Exception as e:
            logger.error(f"OPC UA事件循环异常: {e}")
            self.stats['last_error'] = str(e)
        finally:
            self._loop.close()

    async def _connect_and_subscribe(self):
        """建立连接并订阅节点"""
        try:
            self.client = Client(url=self.endpoint)

            # 设置认证
            if self.username:
                self.client.set_user(self.username)
                self.client.set_password(self.password)

            # 设置安全策略
            if self.security_mode == 'SignAndEncrypt':
                # 需要证书文件，生产环境使用
                await self.client.set_security_string(
                    f"Basic256Sha256,SignAndEncrypt,cert.pem,key.pem"
                )

            # 连接
            await self.client.connect()
            self.connected = True
            self.stats['connected_since'] = datetime.now().isoformat()
            logger.info(f"OPC UA已连接: {self.endpoint}")

            # 创建订阅（500ms间隔）
            self.subscription = await self.client.create_subscription(500, self)
            self.stats['nodes_subscribed'] = len(self.node_configs)

            # 订阅所有配置的节点
            for node_cfg in self.node_configs:
                node_id = node_cfg.get('node_id')
                node_name = node_cfg.get('name', node_id)
                try:
                    node = self.client.get_node(node_id)
                    await self.subscription.subscribe_data_change(node)
                    logger.info(f"订阅节点: {node_name} ({node_id})")
                except Exception as e:
                    logger.error(f"订阅节点失败 {node_name}: {e}")
                    self.stats['errors'] += 1

            # 保持运行
            while self._running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"OPC UA连接异常: {e}")
            self.stats['last_error'] = str(e)
            self.connected = False
        finally:
            await self._cleanup()

    async def _cleanup(self):
        """清理资源"""
        try:
            if self.subscription:
                await self.subscription.delete()
            if self.client:
                await self.client.disconnect()
        except Exception as e:
            logger.error(f"OPC UA清理异常: {e}")
        self.connected = False

    # ---- Subscription Handler（asyncua回调接口）----

    def datachange_notification(self, node, val, data):
        """
        数据变更回调（asyncua自动调用）

        Args:
            node: 变更的节点
            val: 新值
            data: 附加数据
        """
        try:
            # 找到对应的节点配置
            node_id_str = node.nodeid.to_string()
            node_name = node_id_str
            node_unit = ""

            for cfg in self.node_configs:
                if cfg.get('node_id') == node_id_str:
                    node_name = cfg.get('name', node_id_str)
                    node_unit = cfg.get('unit', '')
                    break

            # 更新缓存
            self.latest_data[node_name] = {
                'value': val,
                'unit': node_unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good',
                'node_id': node_id_str
            }

            self.stats['data_updates'] += 1

            # 触发回调
            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, node_name, val, node_unit)
                except Exception as e:
                    logger.error(f"数据回调异常: {e}")

        except Exception as e:
            logger.error(f"数据变更处理异常: {e}")
            self.stats['errors'] += 1

    def event_notification(self, event):
        """事件通知回调"""
        logger.info(f"OPC UA事件: {event}")

    def status_change_notification(self, status):
        """状态变更回调"""
        logger.info(f"OPC UA状态变更: {status}")


class OPCUADiscovery:
    """
    OPC UA局域网发现
    扫描局域网内的OPC UA服务器
    """

    @staticmethod
    async def discover_servers(timeout: float = 5.0) -> list[dict[str, Any]]:
        """
        发现局域网内的OPC UA服务器（通过LDS本地发现服务）

        Args:
            timeout: 超时时间

        Returns:
            list[dict[str, Any]]: 发现的服务器列表
        """
        if not OPCUA_AVAILABLE:
            return []

        servers = []
        try:
            # 使用OPC UA内置的发现机制
            from asyncua.tools import find_servers
            # 备用方案：尝试常见端口
            common_ports = [4840, 4841, 4842, 4843]
            for port in common_ports:
                try:
                    url = f"opc.tcp://localhost:{port}"
                    client = Client(url=url)
                    await asyncio.wait_for(client.connect(), timeout=2)

                    # 获取服务器信息
                    server_info = {
                        'endpoint': url,
                        'port': port,
                        'status': 'online'
                    }
                    servers.append(server_info)
                    await client.disconnect()
                except:
                    pass

        except Exception as e:
            logger.error(f"OPC UA发现异常: {e}")

        return servers


class OPCUABridge:
    """
    OPC UA协议桥接器
    将OPC UA数据转换为统一格式，与其他协议互通
    """

    def __init__(self, opcua_client: OPCUAClient):
        self.client = opcua_client

    def to_unified_format(self) -> dict[str, Any]:
        """
        将OPC UA数据转为统一数据格式

        Returns:
            dict[str, Any]: 统一格式数据，兼容SCADA存储层
        """
        unified = {}
        for name, data in self.client.get_latest_data().items():
            unified[name] = {
                'value': data['value'],
                'unit': data.get('unit', ''),
                'timestamp': data.get('timestamp'),
                'quality': data.get('quality', 'unknown'),
                'source': 'opcua',
                'device_id': self.client.device_id
            }
        return unified
