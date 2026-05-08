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
            'last_error_time': None
        }
        
        # 回调函数
        self._on_data_callback: Callable[..., Any] | None = None
        self._on_alarm_callback: Callable[..., Any] | None = None
        
        # 设置信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
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
    
    def start(self):
        """启动网关"""
        if self.running:
            self.logger.warning("网关已在运行")
            return
        
        self.logger.info(f"启动网关 {self.gateway_id}...")
        self.stats['start_time'] = datetime.now()
        
        # 连接MQTT
        self._connect_mqtt()
        
        # 连接设备
        if not self.connect():
            self.logger.error("设备连接失败")
            return
        
        # 启动轮询线程
        self.running = True
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        
        self.logger.info(f"网关 {self.gateway_id} 已启动")
    
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
    
    def publish_telemetry(self, telemetry: DeviceTelemetry):
        """
        发布遥测数据到MQTT
        
        Args:
            telemetry: 统一物模型数据
        """
        if not self.mqtt_client:
            self.logger.error("MQTT客户端未初始化")
            return
        
        topic = MQTTTopics.get_telemetry_topic(telemetry.DeviceID)
        payload = telemetry.to_json()
        
        try:
            result = self.mqtt_client.publish(topic, payload, qos=1)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                self.logger.error(f"发布失败: {result.rc}")
                self.stats['errors'] += 1
        except Exception as e:
            self.logger.error(f"发布异常: {e}")
            self.stats['errors'] += 1
            self.stats['last_error'] = str(e)
            self.stats['last_error_time'] = datetime.now()
    
    def publish_status(self, device_id: str, online: bool, status: str, 
                       error_code: int = 0, error_message: str = ""):
        """
        发布设备状态到MQTT
        """
        if not self.mqtt_client:
            return
        
        status_msg = DeviceStatus(
            DeviceID=device_id,
            Timestamp=time.time(),
            Online=online,
            Status=status,
            ErrorCode=error_code,
            ErrorMessage=error_message
        )
        
        topic = MQTTTopics.get_status_topic(device_id)
        self.mqtt_client.publish(topic, status_msg.to_json(), qos=1)
    
    def publish_alarm(self, device_id: str, level: str, alarm_type: str, 
                      message: str, value: float = 0, threshold: float = 0):
        """
        发布报警到MQTT
        """
        if not self.mqtt_client:
            return
        
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
        self.mqtt_client.publish(topic, alarm.to_json(), qos=1)
    
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
