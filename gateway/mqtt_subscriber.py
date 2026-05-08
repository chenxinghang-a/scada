"""
MQTT订阅客户端 (MQTT Subscriber)

主系统通过此模块订阅网关发布的标准化数据。
实现第三层：消息总线隔离。

设计原则：
- 异步接收数据
- 自动重连
- 多主题订阅
- 数据分发到各业务模块
"""

import json
import time
import logging
import threading
from typing import Dict, List, Optional, Callable, Any
from datetime import datetime

import paho.mqtt.client as mqtt

from .thing_model import (
    DeviceTelemetry, DeviceStatus, AlarmMessage,
    MQTTTopics, ThingModelValidator
)


class MQTTSubscriber:
    """
    MQTT订阅客户端
    
    订阅网关发布的标准化数据，并分发到各业务模块。
    
    使用示例：
        subscriber = MQTTSubscriber("localhost", 1883)
        subscriber.on_telemetry = my_telemetry_handler
        subscriber.start()
    """
    
    def __init__(self, broker_host: str = "localhost", broker_port: int = 1883,
                 client_id: str = None):
        """
        初始化MQTT订阅客户端
        
        Args:
            broker_host: MQTT Broker地址
            broker_port: MQTT端口
            client_id: 客户端ID（可选）
        """
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id or f"scada_subscriber_{int(time.time())}"
        
        self.logger = logging.getLogger("MQTTSubscriber")
        
        # MQTT客户端
        self._client: Optional[mqtt.Client] = None
        self._connected = False
        
        # 订阅的主题列表
        self._subscribed_topics: List[str] = []
        
        # 回调函数
        self.on_telemetry: Optional[Callable[[DeviceTelemetry], None]] = None
        self.on_status: Optional[Callable[[DeviceStatus], None]] = None
        self.on_alarm: Optional[Callable[[AlarmMessage], None]] = None
        self.on_raw_message: Optional[Callable[[str, str], None]] = None
        
        # 数据缓存（用于查询最新值）
        self._telemetry_cache: Dict[str, DeviceTelemetry] = {}
        self._status_cache: Dict[str, DeviceStatus] = {}
        self._cache_lock = threading.Lock()
        
        # 统计信息
        self.stats = {
            'messages_received': 0,
            'errors': 0,
            'last_message_time': None,
            'connected_since': None
        }
        
        # 运行状态
        self.running = False
    
    def start(self):
        """启动订阅客户端"""
        if self.running:
            self.logger.warning("订阅客户端已在运行")
            return
        
        self.logger.info(f"启动MQTT订阅客户端: {self.broker_host}:{self.broker_port}")
        
        try:
            # 创建MQTT客户端
            self._client = mqtt.Client(client_id=self.client_id)
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message
            
            # 连接Broker
            self._client.connect(self.broker_host, self.broker_port, keepalive=60)
            self._client.loop_start()
            
            self.running = True
            self.logger.info("MQTT订阅客户端已启动")
            
        except Exception as e:
            self.logger.error(f"启动失败: {e}")
            raise
    
    def stop(self):
        """停止订阅客户端"""
        if not self.running:
            return
        
        self.logger.info("停止MQTT订阅客户端...")
        self.running = False
        
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        
        self._connected = False
        self.logger.info("MQTT订阅客户端已停止")
    
    def _on_connect(self, client, userdata, flags, rc):
        """连接成功回调"""
        if rc == 0:
            self.logger.info("MQTT连接成功")
            self._connected = True
            self.stats['connected_since'] = datetime.now()
            
            # 重新订阅所有主题
            self._resubscribe()
        else:
            self.logger.error(f"MQTT连接失败，返回码: {rc}")
    
    def _on_disconnect(self, client, userdata, rc):
        """断开连接回调"""
        self._connected = False
        if rc != 0:
            self.logger.warning(f"MQTT意外断开，返回码: {rc}")
    
    def _on_message(self, client, userdata, msg):
        """消息接收回调"""
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            
            self.stats['messages_received'] += 1
            self.stats['last_message_time'] = datetime.now()
            
            # 调用原始消息回调
            if self.on_raw_message:
                self.on_raw_message(topic, payload)
            
            # 根据主题类型处理
            if '/telemetry' in topic:
                self._handle_telemetry(topic, payload)
            elif '/status' in topic:
                self._handle_status(topic, payload)
            elif '/alarms' in topic:
                self._handle_alarm(topic, payload)
            else:
                self.logger.debug(f"未知主题: {topic}")
                
        except Exception as e:
            self.logger.error(f"处理消息异常: {e}")
            self.stats['errors'] += 1
    
    def _handle_telemetry(self, topic: str, payload: str):
        """处理遥测数据"""
        try:
            data = json.loads(payload)
            
            # 验证数据格式
            is_valid, errors = ThingModelValidator.validate_telemetry(data)
            if not is_valid:
                self.logger.warning(f"遥测数据格式错误: {errors}")
                return
            
            # 转换为对象
            telemetry = DeviceTelemetry.from_dict(data)
            
            # 更新缓存
            with self._cache_lock:
                self._telemetry_cache[telemetry.DeviceID] = telemetry
            
            # 调用回调
            if self.on_telemetry:
                self.on_telemetry(telemetry)
                
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON解析失败: {e}")
        except Exception as e:
            self.logger.error(f"处理遥测数据异常: {e}")
    
    def _handle_status(self, topic: str, payload: str):
        """处理状态数据"""
        try:
            data = json.loads(payload)
            status = DeviceStatus.from_dict(data)
            
            # 更新缓存
            with self._cache_lock:
                self._status_cache[status.DeviceID] = status
            
            # 调用回调
            if self.on_status:
                self.on_status(status)
                
        except Exception as e:
            self.logger.error(f"处理状态数据异常: {e}")
    
    def _handle_alarm(self, topic: str, payload: str):
        """处理报警数据"""
        try:
            data = json.loads(payload)
            alarm = AlarmMessage.from_dict(data)
            
            # 调用回调
            if self.on_alarm:
                self.on_alarm(alarm)
                
        except Exception as e:
            self.logger.error(f"处理报警数据异常: {e}")
    
    def _resubscribe(self):
        """重新订阅所有主题"""
        for topic in self._subscribed_topics:
            self._client.subscribe(topic, qos=1)
            self.logger.info(f"重新订阅: {topic}")
    
    def subscribe_telemetry(self, device_id: str = "#"):
        """
        订阅遥测数据
        
        Args:
            device_id: 设备ID，"#"表示所有设备
        """
        topic = MQTTTopics.get_telemetry_topic(device_id)
        self._subscribe(topic)
    
    def subscribe_status(self, device_id: str = "#"):
        """订阅设备状态"""
        topic = MQTTTopics.get_status_topic(device_id)
        self._subscribe(topic)
    
    def subscribe_alarms(self, level: str = "#"):
        """订阅报警"""
        topic = MQTTTopics.get_alarm_topic(level)
        self._subscribe(topic)
    
    def subscribe_all(self):
        """订阅所有主题"""
        self.subscribe_telemetry()
        self.subscribe_status()
        self.subscribe_alarms()
    
    def _subscribe(self, topic: str):
        """订阅单个主题"""
        if topic not in self._subscribed_topics:
            self._subscribed_topics.append(topic)
            
            if self._client and self._connected:
                self._client.subscribe(topic, qos=1)
                self.logger.info(f"订阅: {topic}")
    
    def unsubscribe(self, topic: str):
        """取消订阅"""
        if topic in self._subscribed_topics:
            self._subscribed_topics.remove(topic)
            
            if self._client and self._connected:
                self._client.unsubscribe(topic)
                self.logger.info(f"取消订阅: {topic}")
    
    def get_latest_telemetry(self, device_id: str) -> Optional[DeviceTelemetry]:
        """获取设备最新的遥测数据"""
        with self._cache_lock:
            return self._telemetry_cache.get(device_id)
    
    def get_latest_status(self, device_id: str) -> Optional[DeviceStatus]:
        """获取设备最新的状态"""
        with self._cache_lock:
            return self._status_cache.get(device_id)
    
    def get_all_telemetry(self) -> Dict[str, DeviceTelemetry]:
        """获取所有设备的最新遥测数据"""
        with self._cache_lock:
            return self._telemetry_cache.copy()
    
    def get_all_status(self) -> Dict[str, DeviceStatus]:
        """获取所有设备的最新状态"""
        with self._cache_lock:
            return self._status_cache.copy()
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        stats = self.stats.copy()
        stats['connected'] = self._connected
        stats['subscribed_topics'] = len(self._subscribed_topics)
        stats['cached_devices'] = len(self._telemetry_cache)
        return stats


class MQTTDataDistributor:
    """
    MQTT数据分发器
    
    将接收到的MQTT数据分发到各业务模块。
    这是连接消息总线和业务逻辑的桥梁。
    """
    
    def __init__(self, subscriber: MQTTSubscriber):
        self.subscriber = subscriber
        self.logger = logging.getLogger("MQTTDataDistributor")
        
        # 业务模块引用
        self._modules: Dict[str, Any] = {}
        
        # 设置回调
        self.subscriber.on_telemetry = self._on_telemetry
        self.subscriber.on_status = self._on_status
        self.subscriber.on_alarm = self._on_alarm
    
    def register_module(self, name: str, module: Any):
        """
        注册业务模块
        
        Args:
            name: 模块名称 (oee, predictive, spc, energy, alarm)
            module: 模块实例
        """
        self._modules[name] = module
        self.logger.info(f"注册模块: {name}")
    
    def _on_telemetry(self, telemetry: DeviceTelemetry):
        """分发遥测数据到各模块"""
        for name, module in self._modules.items():
            try:
                if hasattr(module, 'update_from_telemetry'):
                    module.update_from_telemetry(telemetry)
                elif hasattr(module, 'feed_data'):
                    # 兼容旧接口
                    for reg_name, metric in telemetry.Metrics.items():
                        module.feed_data(telemetry.DeviceID, reg_name, metric['value'])
            except Exception as e:
                self.logger.error(f"模块 {name} 处理异常: {e}")
    
    def _on_status(self, status: DeviceStatus):
        """分发状态数据"""
        for name, module in self._modules.items():
            try:
                if hasattr(module, 'update_device_status'):
                    module.update_device_status(
                        status.DeviceID,
                        status.Online,
                        status.Status
                    )
            except Exception as e:
                self.logger.error(f"模块 {name} 处理状态异常: {e}")
    
    def _on_alarm(self, alarm: AlarmMessage):
        """分发报警数据"""
        alarm_module = self._modules.get('alarm')
        if alarm_module and hasattr(alarm_module, 'process_alarm'):
            try:
                alarm_module.process_alarm(alarm)
            except Exception as e:
                self.logger.error(f"报警模块处理异常: {e}")


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 创建订阅客户端
    subscriber = MQTTSubscriber("localhost", 1883)
    
    # 设置回调
    def on_telemetry(telemetry):
        print(f"收到遥测数据: {telemetry.DeviceID}")
        for name, metric in telemetry.Metrics.items():
            print(f"  {name}: {metric['value']} {metric['unit']}")
    
    subscriber.on_telemetry = on_telemetry
    
    # 订阅所有主题
    subscriber.subscribe_all()
    
    # 启动
    subscriber.start()
    
    print("等待数据... (按 Ctrl+C 停止)")
    
    try:
        while True:
            import time
            time.sleep(1)
            stats = subscriber.get_stats()
            print(f"统计: {stats}")
    except KeyboardInterrupt:
        print("正在停止...")
    finally:
        subscriber.stop()
