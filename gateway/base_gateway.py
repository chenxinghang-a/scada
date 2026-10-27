"""
网关基类 (Base Gateway)

所有协议网关必须继承此基类，实现统一的接口。
网关负责：
1. 与设备通信（协议特定）
2. 将原始数据转换为统一物模型
3. 通过MQTT发布标准化数据
4. 处理连接异常和重连

设计原则：
- 独立进程运行，故障不影响主系统
- 自动重连机制
- 心跳检测
- 优雅关闭
"""

import json
import ssl
import time
import logging
import threading
import signal
from abc import ABC, abstractmethod
from typing import Any, Callable
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

from .thing_model import (
    DeviceTelemetry, DeviceStatus, AlarmMessage,
    ThingModelConverter, MQTTTopics, DataQuality
)

logger = logging.getLogger(__name__)

# 全局信号处理：所有网关实例共享，避免互相覆盖
_active_gateways: list = []
_signal_registered = False


def _global_signal_handler(signum, frame):
    """全局信号处理：停止所有活跃网关"""
    logger.info(f"接收到信号 {signum}，正在关闭所有网关...")
    for gw in _active_gateways:
        try:
            gw.stop()
        except Exception as e:
            logger.error(f"关闭网关失败: {e}")


class BaseGateway(ABC):
    """
    网关基类

    所有协议网关必须实现以下方法：
    - connect(): 连接设备
    - disconnect(): 断开设备连接
    - read_data(): 读取设备数据
    - convert_to_telemetry(): 转换为统一物模型
    """

    def __init__(self, config: dict[str, Any]):
        """
        初始化网关

        Args:
            config: 网关配置，包含：
                - gateway_id: 网关唯一标识
                - mqtt_broker: MQTT Broker地址
                - mqtt_port: MQTT端口
                - devices: 设备列表配置
                - poll_interval: 轮询间隔（秒）
                - reconnect_interval: 重连间隔（秒）
                - max_reconnect_attempts: 最大重连次数
        """
        self.config = config
        self.gateway_id = config.get('gateway_id', 'gateway_001')
        self.logger = logging.getLogger(f"Gateway.{self.gateway_id}")

        # MQTT配置
        self.mqtt_broker = config.get('mqtt_broker', 'localhost')
        self.mqtt_port = config.get('mqtt_port', 1883)
        self.mqtt_client: mqtt.Client | None = None

        # 设备配置
        self.devices_config = config.get('devices', [])

        # 轮询配置
        self.poll_interval = config.get('poll_interval', 5.0)
        self.reconnect_interval = config.get('reconnect_interval', 10.0)
        self.max_reconnect_attempts = config.get('max_reconnect_attempts', 0)  # 0=无限重试

        # 运行状态
        self.running = False
        self.connected_devices: dict[str, bool] = {}
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # 统计信息
        self.stats: dict[str, Any] = {
            'start_time': None,
            'messages_published': 0,
            'errors': 0,
            'last_error': None,
            'last_error_time': None,
            # 设备连接结果（round 294）：部分成功也要跑，因此必须能读出"连上几台"，
            # 否则"网关在跑但少了三台设备的数据"只能靠人工对账发现。
            'devices_connected': 0,
            'devices_failed': 0,
            # 单点读取失败（round 295）：此前只打日志、stats 一直是 0 ——
            # `/stats` 上"errors=0"与"每次都在读挂"可以同时成立。
            'read_failures': 0,
            'last_read_error': None,
            'last_read_error_time': None,
        }

        # 回调函数
        self._on_data_callback: Callable[..., Any] | None = None
        self._on_alarm_callback: Callable[..., Any] | None = None

        # 注册到全局信号处理器（避免多个实例互相覆盖）
        #
        # round 292 实测：这里缺 `global _signal_registered` —— 函数体里对它赋值
        # 会让它在整个 `__init__` 里变成**局部变量**，于是 `if not _signal_registered`
        # 读的是未初始化局部变量，**任何网关一实例化就抛 UnboundLocalError**。
        # 换句话说：`gateway/` 这 4400 行在运行时根本起不来，而此前没有任何
        # 一条测试实例化过 BaseGateway（只测了各协议的 decode 函数）。
        global _signal_registered
        _active_gateways.append(self)
        if not _signal_registered:
            try:
                signal.signal(signal.SIGINT, _global_signal_handler)
                signal.signal(signal.SIGTERM, _global_signal_handler)
                _signal_registered = True
            except ValueError as e:
                # 非主线程里注册信号处理器会抛 ValueError —— 那不是错误，
                # 网关在子线程里构造是合法用法（run_gateway 就可能在子线程起）。
                logger.debug("非主线程，跳过信号处理器注册: %s", e)

    def _signal_handler(self, signum, frame):
        """信号处理函数，用于优雅关闭"""
        self.logger.info(f"接收到信号 {signum}，正在关闭...")
        self.stop()

    @abstractmethod
    def connect(self) -> bool:
        """
        连接所有设备

        Returns:
            bool: 是否全部连接成功
        """
        pass

    @abstractmethod
    def disconnect(self):
        """断开所有设备连接"""
        pass

    @abstractmethod
    def read_device_data(self, device_id: str) -> dict[str, float] | None:
        """
        读取单个设备数据

        Args:
            device_id: 设备ID

        Returns:
            dict[str, float]: 寄存器数据 {register_name: value}
            None: 读取失败
        """
        pass

    @abstractmethod
    def convert_to_telemetry(self, device_id: str, raw_data: dict[str, float]) -> DeviceTelemetry:
        """
        将原始数据转换为统一物模型

        Args:
            device_id: 设备ID
            raw_data: 原始数据

        Returns:
            DeviceTelemetry: 统一物模型
        """
        pass

    def start(self) -> bool:
        """启动网关。

        Returns:
            bool: **真的起来了**（MQTT 连上 + 设备全连上 + 轮询线程已起）才返回 True。

        round 293：此前 `start()` 无返回值，且：
          * `_connect_mqtt()` 失败会**抛出**异常，而 `run_gateway` 的
            `try/except` 只打一行日志就继续 —— 进程照常存活；
          * `connect()` 返回 False 时这里只 `logger.error` 然后 `return`；
        两者叠加的结果：MQTT/设备一个都没连上时，**进程仍然活着**、
        最后一行日志还是「网关服务已启动」—— 服务管理器和运维都以为它在跑。
        现在把"起没起来"如实返回，由调用方决定退出码。
        """
        if self.running:
            self.logger.warning("网关已在运行")
            return True

        self.logger.info(f"启动网关 {self.gateway_id}...")
        self.stats['start_time'] = datetime.now()

        # 连接MQTT（失败即"没起来"）
        try:
            self._connect_mqtt()
        except Exception as e:
            self.stats['start_time'] = None
            self._note_publish_failure(f"MQTT 连接失败，网关未启动: {e}")
            return False

        # 连接设备
        #
        # round 294：**部分成功也要跑**。
        # 各协议网关的 `connect()` 返回的是「是否**全部**连上」（`all_connected`），
        # 而 293 起 `start()` 会照这个值判生死 —— 于是"10 台设备只挂了 1 台"
        # 会让整个网关不启动，另外 9 台的好数据一起没了。
        # 正确语义（工业现场）：能连上的先跑，连不上的标 fault 并在轮询里重连；
        # 只有**一台都没连上**才算这个网关起不来。
        all_connected = self.connect()
        configured = [d.get('device_id') for d in self.devices_config if d.get('device_id')]
        connected = [d for d, ok in self.connected_devices.items() if ok]
        self.stats['devices_connected'] = len(connected)
        self.stats['devices_failed'] = max(0, len(configured) - len(connected))

        if configured and not connected:
            self.stats['start_time'] = None
            self._note_publish_failure(
                f"设备全部连接失败（0/{len(configured)}），网关未启动")
            return False

        if configured and not all_connected:
            # 部分成功：继续跑，但必须说清楚哪几台没连上（否则"少了一台的数据"
            # 永远只能靠对账发现）。
            failed_devices = [d for d in configured if not self.connected_devices.get(d)]
            self.logger.warning(
                "部分设备连接失败：%d/%d 连上，网关继续运行；未连上: %s",
                len(connected), len(configured), failed_devices)

        # 启动轮询线程
        self.running = True
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        self.logger.info(f"网关 {self.gateway_id} 已启动")
        return True

    def stop(self):
        """停止网关"""
        if not self.running:
            return

        self.logger.info(f"停止网关 {self.gateway_id}...")
        self.running = False
        self._stop_event.set()

        # 等待轮询线程结束
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)

        # 断开设备
        self.disconnect()

        # 断开MQTT
        self._disconnect_mqtt()

        self.logger.info(f"网关 {self.gateway_id} 已停止")

    def _connect_mqtt(self):
        """连接MQTT Broker"""
        try:
            self.mqtt_client = mqtt.Client(client_id=f"{self.gateway_id}_{int(time.time())}")
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_client.on_publish = self._on_mqtt_publish

            # TLS/SSL配置
            tls_config = self.config.get('mqtt_tls', {})
            if tls_config.get('enabled', False):
                tls_context = ssl.create_default_context()

                ca_cert = tls_config.get('ca_cert')
                if ca_cert:
                    tls_context.load_verify_locations(ca_cert)
                else:
                    tls_context.check_hostname = False
                    tls_context.verify_mode = ssl.CERT_NONE

                client_cert = tls_config.get('client_cert')
                client_key = tls_config.get('client_key')
                if client_cert and client_key:
                    tls_context.load_cert_chain(client_cert, client_key)

                if tls_config.get('insecure', False):
                    tls_context.check_hostname = False
                    tls_context.verify_mode = ssl.CERT_NONE

                self.mqtt_client.tls_set_context(tls_context)
                self.logger.info("MQTT TLS已启用")

            self.logger.info(f"连接MQTT Broker: {self.mqtt_broker}:{self.mqtt_port}")
            self.mqtt_client.connect(self.mqtt_broker, self.mqtt_port, keepalive=60)
            self.mqtt_client.loop_start()

        except Exception as e:
            self.logger.error(f"MQTT连接失败: {e}")
            raise

    def _disconnect_mqtt(self):
        """断开MQTT连接"""
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.mqtt_client = None

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT连接回调"""
        if rc == 0:
            self.logger.info("MQTT连接成功")
        else:
            self.logger.error(f"MQTT连接失败，返回码: {rc}")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT断开回调"""
        if rc != 0:
            self.logger.warning(f"MQTT意外断开，返回码: {rc}")

    def _on_mqtt_publish(self, client, userdata, mid):
        """MQTT发布回调"""
        self.stats['messages_published'] += 1

    def _note_read_failure(self, device_id: str, register_name: str, reason: str) -> None:
        """单点读取失败记账（round 295）。

        为什么单独记：一个设备的寄存器**逐点**读，失败的点点只打一行日志就从
        结果里消失了 —— `stats['errors']` 一直是 0，`/stats` 看起来完全健康，
        而实际上"这台设备的 10 个点里 9 个读不到"。
        与发布失败分开计数，是因为两者的处置方式不同：发布失败要回填，
        读取失败要重连/降级质量码。

        ⚠️ 记账本身**绝不许把读取路径搞崩**：这段代码被调用的位置全是
        "已经失败、马上要 return None" 的分支，如果这里抛异常，
        "读不到"就会变成"抛出去" —— 上层从"少一个点"变成"整台设备异常"。
        所以 `stats` 缺失（裸对象 / 未走 __init__ 的测试替身）时静默跳过。
        """
        stats = getattr(self, 'stats', None)
        if stats is None:
            return
        stats['read_failures'] = stats.get('read_failures', 0) + 1
        stats['last_read_error'] = f"{device_id}/{register_name}: {reason}"
        stats['last_read_error_time'] = datetime.now()

    def _note_publish_failure(self, message: str) -> None:
        """统一的发布失败记账（日志 + 计数 + 最后一次错误）。

        round 292：此前三条发布通道各有各的写法 —— 一条只计数不返回、
        两条**连返回码都不看**。失败必须留痕，否则"网关发出去了没有"
        永远只能靠猜。
        """
        self.logger.error(message)
        self.stats['errors'] += 1
        self.stats['last_error'] = message
        self.stats['last_error_time'] = datetime.now()

    def _publish(self, topic: str, payload: str, what: str) -> bool:
        """统一的 MQTT 发布：返回值 = **真的发出去了吗**，不是"我尝试过了"。

        判据（承接 283-291 同一族）：
          * `publish()` 返回码不是 `MQTT_ERR_SUCCESS` → 失败；
          * 抛异常 → 失败；
          * 客户端没初始化 → 失败（原先 `publish_status`/`publish_alarm`
            在这里**静默 return**，连计数都没有）。
        """
        if not self.mqtt_client:
            self._note_publish_failure(f"{what} 发布失败: MQTT客户端未初始化")
            return False

        try:
            result = self.mqtt_client.publish(topic, payload, qos=1)
        except Exception as e:
            self._note_publish_failure(f"{what} 发布异常: {e}")
            return False

        rc = getattr(result, 'rc', None)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            self._note_publish_failure(f"{what} 发布失败: rc={rc}")
            return False
        return True

    def publish_telemetry(self, telemetry: DeviceTelemetry) -> bool:
        """
        发布遥测数据到MQTT

        Args:
            telemetry: 统一物模型数据

        Returns:
            bool: 是否真的发布成功（round 292 起有返回值）
        """
        topic = MQTTTopics.get_telemetry_topic(telemetry.DeviceID)
        return self._publish(topic, telemetry.to_json(), '遥测')

    def publish_status(self, device_id: str, online: bool, status: str, 
                       error_code: int = 0, error_message: str = "") -> bool:
        """
        发布设备状态到MQTT

        Returns:
            bool: 是否真的发布成功（round 292：此前**完全不看返回码**）
        """
        status_msg = DeviceStatus(
            DeviceID=device_id,
            Timestamp=time.time(),
            Online=online,
            Status=status,
            ErrorCode=error_code,
            ErrorMessage=error_message
        )

        topic = MQTTTopics.get_status_topic(device_id)
        return self._publish(topic, status_msg.to_json(), f'状态({device_id})')

    def publish_alarm(self, device_id: str, level: str, alarm_type: str, 
                      message: str, value: float = 0, threshold: float = 0) -> bool:
        """
        发布报警到MQTT

        Returns:
            bool: 是否真的发布成功（round 292：此前**完全不看返回码**）
        """
        alarm = AlarmMessage(
            AlarmID=f"{device_id}_{int(time.time())}",
            DeviceID=device_id,
            Timestamp=time.time(),
            Level=level,
            Type=alarm_type,
            Message=message,
            Value=value,
            Threshold=threshold
        )

        topic = MQTTTopics.get_alarm_topic(level)
        return self._publish(topic, alarm.to_json(), f'报警({device_id}/{level})')

    def _poll_loop(self):
        """数据轮询循环"""
        self.logger.info(f"开始数据轮询，间隔: {self.poll_interval}秒")

        while self.running and not self._stop_event.is_set():
            try:
                # 遍历所有设备
                for device_config in self.devices_config:
                    if not self.running:
                        break

                    device_id = device_config.get('device_id')
                    if not device_id:
                        continue

                    try:
                        # 读取数据
                        raw_data = self.read_device_data(device_id)

                        if raw_data is not None:
                            # 转换为统一物模型
                            telemetry = self.convert_to_telemetry(device_id, raw_data)

                            # 发布到MQTT
                            self.publish_telemetry(telemetry)

                            # 更新设备状态
                            self.connected_devices[device_id] = True
                            self.publish_status(device_id, True, "running")

                            # 调用数据回调
                            if self._on_data_callback:
                                self._on_data_callback(telemetry)
                        else:
                            # 读取失败
                            self.connected_devices[device_id] = False
                            self.publish_status(device_id, False, "fault", 
                                              error_code=1, error_message="数据读取失败")

                    except Exception as e:
                        self.logger.error(f"设备 {device_id} 处理异常: {e}")
                        self.stats['errors'] += 1
                        self.stats['last_error'] = str(e)
                        self.stats['last_error_time'] = datetime.now()

                # 等待下次轮询
                self._stop_event.wait(self.poll_interval)

            except Exception as e:
                self.logger.error(f"轮询循环异常: {e}")
                self.stats['errors'] += 1
                self._stop_event.wait(self.reconnect_interval)

    def set_on_data_callback(self, callback: Callable[[DeviceTelemetry], None]):
        """设置数据回调"""
        self._on_data_callback = callback

    def set_on_alarm_callback(self, callback: Callable[[AlarmMessage], None]):
        """设置报警回调"""
        self._on_alarm_callback = callback

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        stats = self.stats.copy()
        stats['running'] = self.running
        stats['connected_devices'] = self.connected_devices.copy()
        if stats['start_time']:
            stats['uptime'] = (datetime.now() - stats['start_time']).total_seconds()
        return stats

    def is_device_connected(self, device_id: str) -> bool:
        """检查设备是否连接"""
        return self.connected_devices.get(device_id, False)

    def get_connected_devices(self) -> list[str]:
        """获取已连接的设备列表"""
        return [did for did, connected in self.connected_devices.items() if connected]
