"""
MQTT数据采集模块
实现MQTT协议的数据采集功能
"""

import json
import logging
import threading
from typing import Dict, List, Callable, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    logger.warning("paho-mqtt未安装，MQTT功能不可用。请运行: pip install paho-mqtt")


class MQTTClient:
    """
    MQTT数据采集客户端
    支持订阅多个主题，解析JSON数据并转发到数据库
    """
    
    def __init__(self, broker_host: str = 'localhost', broker_port: int = 1883,
                 client_id: str = None, username: str = None, password: str = None):
        """
        初始化MQTT客户端
        
        Args:
            broker_host: MQTT Broker地址
            broker_port: MQTT Broker端口
            client_id: 客户端ID（可选）
            username: 用户名（可选）
            password: 密码（可选）
        """
        if not MQTT_AVAILABLE:
            raise ImportError("paho-mqtt未安装，请运行: pip install paho-mqtt")
        
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id or f'scada_{datetime.now().strftime("%H%M%S")}'
        
        # 创建MQTT客户端
        self.client = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv311)
        
        # 设置认证
        if username:
            self.client.username_pw_set(username, password)
        
        # 设置回调
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe
        
        # 数据回调函数
        self._data_callbacks: List[Callable] = []
        
        # 订阅主题列表
        self._subscriptions: Dict[str, int] = {}  # topic -> qos
        
        # 连接状态
        self._connected = False
        self._lock = threading.Lock()
        
        # 统计信息
        self.stats = {
            'messages_received': 0,
            'messages_parsed': 0,
            'parse_errors': 0,
            'connected_since': None,
            'last_message_time': None
        }
        
        logger.info(f"MQTT客户端初始化: {broker_host}:{broker_port}")
    
    def connect(self) -> bool:
        """
        连接到MQTT Broker
        
        Returns:
            bool: 连接是否成功
        """
        try:
            logger.info(f"正在连接MQTT Broker: {self.broker_host}:{self.broker_port}")
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            return True
        except Exception as e:
            logger.error(f"MQTT连接失败: {e}")
            return False
    
    def disconnect(self):
        """断开MQTT连接"""
        try:
            self.client.loop_stop()
            self.client.disconnect()
            self._connected = False
            logger.info("MQTT连接已断开")
        except Exception as e:
            logger.error(f"MQTT断开失败: {e}")
    
    def subscribe(self, topic: str, qos: int = 1) -> bool:
        """
        订阅主题
        
        Args:
            topic: MQTT主题
            qos: 服务质量等级 (0, 1, 2)
            
        Returns:
            bool: 订阅是否成功
        """
        self._subscriptions[topic] = qos
        
        if self._connected:
            result, mid = self.client.subscribe(topic, qos)
            if result == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"已订阅主题: {topic} (QoS: {qos})")
                return True
            else:
                logger.error(f"订阅失败: {topic}, 错误码: {result}")
                return False
        else:
            logger.info(f"主题已记录，连接后自动订阅: {topic}")
            return True
    
    def unsubscribe(self, topic: str):
        """取消订阅"""
        if topic in self._subscriptions:
            del self._subscriptions[topic]
        
        if self._connected:
            self.client.unsubscribe(topic)
            logger.info(f"已取消订阅: {topic}")
    
    def add_data_callback(self, callback: Callable):
        """
        添加数据回调函数
        
        Args:
            callback: 回调函数，签名: callback(device_id, register_name, value, timestamp, unit)
        """
        self._data_callbacks.append(callback)
    
    def publish(self, topic: str, payload: dict, qos: int = 1) -> bool:
        """
        发布消息
        
        Args:
            topic: 主题
            payload: 消息内容（字典）
            qos: 服务质量
            
        Returns:
            bool: 发布是否成功
        """
        if not self._connected:
            logger.warning("MQTT未连接，无法发布")
            return False
        
        try:
            message = json.dumps(payload, ensure_ascii=False)
            result = self.client.publish(topic, message, qos=qos)
            return result.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error(f"MQTT发布失败: {e}")
            return False
    
    def get_status(self) -> Dict:
        """获取客户端状态"""
        return {
            'connected': self._connected,
            'broker': f'{self.broker_host}:{self.broker_port}',
            'client_id': self.client_id,
            'subscriptions': list(self._subscriptions.keys()),
            'stats': self.stats.copy()
        }
    
    def _on_connect(self, client, userdata, flags, rc):
        """连接成功回调"""
        if rc == 0:
            self._connected = True
            self.stats['connected_since'] = datetime.now().isoformat()
            logger.info(f"MQTT连接成功: {self.broker_host}:{self.broker_port}")
            
            # 自动订阅已记录的主题
            for topic, qos in self._subscriptions.items():
                client.subscribe(topic, qos)
                logger.info(f"自动订阅: {topic}")
        else:
            logger.error(f"MQTT连接失败，返回码: {rc}")
    
    def _on_disconnect(self, client, userdata, rc):
        """断开连接回调"""
        self._connected = False
        if rc != 0:
            logger.warning(f"MQTT意外断开，返回码: {rc}")
        else:
            logger.info("MQTT已断开连接")
    
    def _on_message(self, client, userdata, msg):
        """消息接收回调"""
        try:
            self.stats['messages_received'] += 1
            self.stats['last_message_time'] = datetime.now().isoformat()
            
            # 解析消息
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            
            # 尝试解析JSON
            try:
                data = json.loads(payload)
                self.stats['messages_parsed'] += 1
                self._process_message(topic, data)
            except json.JSONDecodeError:
                # 非JSON格式，尝试简单解析
                self._process_raw_message(topic, payload)
                
        except Exception as e:
            logger.error(f"处理MQTT消息失败: {e}")
            self.stats['parse_errors'] += 1
    
    def _on_subscribe(self, client, userdata, mid, granted_qos):
        """订阅成功回调"""
        logger.debug(f"订阅确认: mid={mid}, QoS={granted_qos}")
    
    def _process_message(self, topic: str, data: dict):
        """
        处理JSON格式消息
        
        支持的消息格式:
        1. {"device_id": "xxx", "register": "temperature", "value": 25.5, "unit": "°C"}
        2. {"temperature": 25.5, "pressure": 0.101}  (自动从topic提取device_id)
        3. {"device_id": "xxx", "data": {"temperature": 25.5, "pressure": 0.101}}
        """
        timestamp = datetime.now()
        
        # 格式1: 完整格式
        if 'device_id' in data and 'register' in data:
            device_id = data['device_id']
            register_name = data['register']
            value = float(data.get('value', 0))
            unit = data.get('unit', '')
            
            self._notify_callbacks(device_id, register_name, value, timestamp, unit)
        
        # 格式3: 嵌套数据格式
        elif 'device_id' in data and 'data' in data:
            device_id = data['device_id']
            for register_name, value in data['data'].items():
                if isinstance(value, (int, float)):
                    self._notify_callbacks(device_id, register_name, float(value), timestamp, '')
        
        # 格式2: 简单键值对
        else:
            # 从topic提取device_id
            device_id = topic.split('/')[-1] if '/' in topic else topic
            for register_name, value in data.items():
                if isinstance(value, (int, float)):
                    self._notify_callbacks(device_id, register_name, float(value), timestamp, '')
    
    def _process_raw_message(self, topic: str, payload: str):
        """处理非JSON格式消息"""
        try:
            # 尝试解析 "key=value" 格式
            if '=' in payload:
                parts = payload.split('=')
                if len(parts) == 2:
                    device_id = topic.split('/')[-1] if '/' in topic else topic
                    register_name = parts[0].strip()
                    value = float(parts[1].strip())
                    self._notify_callbacks(device_id, register_name, value, datetime.now(), '')
            else:
                # 尝试直接解析为数值
                value = float(payload.strip())
                device_id = topic.split('/')[-1] if '/' in topic else topic
                self._notify_callbacks(device_id, 'value', value, datetime.now(), '')
        except (ValueError, IndexError):
            logger.debug(f"无法解析原始消息: {topic} = {payload}")
    
    def _notify_callbacks(self, device_id: str, register_name: str, 
                         value: float, timestamp: datetime, unit: str):
        """通知所有回调函数"""
        for callback in self._data_callbacks:
            try:
                callback(device_id, register_name, value, timestamp, unit)
            except Exception as e:
                logger.error(f"数据回调执行失败: {e}")


class MQTTDeviceManager:
    """
    MQTT设备管理器
    管理多个MQTT设备的数据采集
    """
    
    def __init__(self, config: dict):
        """
        初始化MQTT设备管理器
        
        Args:
            config: MQTT配置字典
                {
                    'broker_host': 'localhost',
                    'broker_port': 1883,
                    'username': None,
                    'password': None,
                    'topics': [
                        {'topic': 'scada/temp_sensor/#', 'qos': 1},
                        {'topic': 'scada/pressure/#', 'qos': 1}
                    ]
                }
        """
        self.config = config
        self.client = None
        self._running = False
    
    def start(self, data_callback: Callable) -> bool:
        """
        启动MQTT数据采集
        
        Args:
            data_callback: 数据回调函数
            
        Returns:
            bool: 是否启动成功
        """
        try:
            self.client = MQTTClient(
                broker_host=self.config.get('broker_host', 'localhost'),
                broker_port=self.config.get('broker_port', 1883),
                username=self.config.get('username'),
                password=self.config.get('password')
            )
            
            # 添加数据回调
            self.client.add_data_callback(data_callback)
            
            # 订阅主题
            topics = self.config.get('topics', [])
            for topic_config in topics:
                topic = topic_config.get('topic', '')
                qos = topic_config.get('qos', 1)
                if topic:
                    self.client.subscribe(topic, qos)
            
            # 连接
            if self.client.connect():
                self._running = True
                logger.info("MQTT设备管理器启动成功")
                return True
            else:
                logger.error("MQTT设备管理器启动失败")
                return False
                
        except Exception as e:
            logger.error(f"MQTT设备管理器启动失败: {e}")
            return False
    
    def stop(self):
        """停止MQTT数据采集"""
        if self.client:
            self.client.disconnect()
            self._running = False
            logger.info("MQTT设备管理器已停止")
    
    def get_status(self) -> Dict:
        """获取状态"""
        if self.client:
            return self.client.get_status()
        return {'connected': False, 'broker': 'N/A'}
