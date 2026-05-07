"""
MQTT数据采集模块
实现MQTT协议的数据采集功能
"""

import json
import logging
import threading
from typing import Dict, List, Callable, Optional, Any
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
    
    构造函数统一接受config字典，与其他协议客户端接口一致。
    config格式:
        {
            'id': 'mqtt_device_01',
            'name': 'MQTT IoT传感器',
            'protocol': 'mqtt',
            'host': 'localhost',
            'port': 1883,
            'username': None,
            'password': None,
            'topics': [
                {'topic': 'factory/temperature', 'qos': 1, 'name': 'temperature', 'unit': '°C'}
            ]
        }
    
    数据回调签名: callback(device_id, register_name, value, unit)
        - 与OPCUA/REST客户端回调签名一致
    """
    
    def __init__(self, config: Dict[str, Any] = None, **kwargs):
        """
        初始化MQTT客户端
        
        Args:
            config: 设备配置字典（推荐方式，与其他协议一致）
            **kwargs: 兼容旧的独立参数方式 (broker_host, broker_port, username, password)
        """
        if not MQTT_AVAILABLE:
            raise ImportError("paho-mqtt未安装，请运行: pip install paho-mqtt")
        
        # 统一从config提取参数
        if config:
            self.config = config
            self.device_id = config.get('id', 'mqtt_device')
            self.device_name = config.get('name', 'MQTT设备')
            self.broker_host = config.get('host', 'localhost')
            self.broker_port = config.get('port', 1883)
            username = config.get('username')
            password = config.get('password')
            self.topics_config = config.get('topics', [])
        else:
            # 兼容旧的独立参数方式
            self.config = kwargs
            self.device_id = kwargs.get('client_id', 'mqtt_device')
            self.device_name = kwargs.get('client_id', 'MQTT设备')
            self.broker_host = kwargs.get('broker_host', 'localhost')
            self.broker_port = kwargs.get('broker_port', 1883)
            username = kwargs.get('username')
            password = kwargs.get('password')
            self.topics_config = []
        
        self.client_id = f'scada_{self.device_id}_{datetime.now().strftime("%H%M%S")}'
        
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
        
        # 订阅主题列表: {topic: qos}
        self._subscriptions: Dict[str, int] = {}
        
        # 连接状态
        self.connected = False
        self._lock = threading.Lock()
        
        # 统计信息
        self.stats = {
            'messages_received': 0,
            'messages_parsed': 0,
            'parse_errors': 0,
            'connected_since': None,
            'last_message_time': None
        }
        
        logger.info(f"MQTT客户端初始化: {self.broker_host}:{self.broker_port} [{self.device_id}]")
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调函数（签名: callback(device_id, name, value, unit)）"""
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        """
        连接到MQTT Broker
        
        注意: paho-mqtt的connect()只发起TCP连接，MQTT握手在loop_start()的后台线程中完成。
        连接成功后on_connect回调才会设置self.connected=True。
        
        Returns:
            bool: connect()调用是否成功（不代表MQTT握手完成）
        """
        try:
            logger.info(f"正在连接MQTT Broker: {self.broker_host}:{self.broker_port}")
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            return True
        except Exception as e:
            logger.error(f"MQTT连接失败: {e}")
            self.stats['last_error'] = str(e)
            return False
    
    def disconnect(self):
        """断开MQTT连接"""
        try:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            logger.info("MQTT连接已断开")
        except Exception as e:
            logger.error(f"MQTT断开失败: {e}")
    
    def subscribe(self, topic: str, qos: int = 1) -> bool:
        """订阅主题"""
        self._subscriptions[topic] = qos
        
        if self.connected:
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
        
        if self.connected:
            self.client.unsubscribe(topic)
            logger.info(f"已取消订阅: {topic}")
    
    def publish(self, topic: str, payload: dict, qos: int = 1) -> bool:
        """发布消息"""
        if not self.connected:
            logger.warning("MQTT未连接，无法发布")
            return False
        
        try:
            message = json.dumps(payload, ensure_ascii=False)
            result = self.client.publish(topic, message, qos=qos)
            return result.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error(f"MQTT发布失败: {e}")
            return False
    
    def get_latest_data(self) -> Dict[str, Dict]:
        """获取最新数据（与其他协议客户端接口统一，MQTT无缓存返回空）"""
        return {}
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息（与其他协议客户端接口统一）"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'broker': f'{self.broker_host}:{self.broker_port}',
            'client_id': self.client_id,
            'subscriptions': list(self._subscriptions.keys()),
            **self.stats
        }
    
    def get_status(self) -> Dict:
        """获取客户端状态"""
        return {
            'connected': self.connected,
            'broker': f'{self.broker_host}:{self.broker_port}',
            'client_id': self.client_id,
            'subscriptions': list(self._subscriptions.keys()),
            'stats': self.stats.copy()
        }
    
    # ---- paho-mqtt 回调 ----
    
    def _on_connect(self, client, userdata, flags, rc):
        """连接成功回调"""
        if rc == 0:
            self.connected = True
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
        self.connected = False
        if rc != 0:
            logger.warning(f"MQTT意外断开，返回码: {rc}")
        else:
            logger.info("MQTT已断开连接")
    
    def _on_message(self, client, userdata, msg):
        """消息接收回调"""
        try:
            self.stats['messages_received'] += 1
            self.stats['last_message_time'] = datetime.now().isoformat()
            
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            
            try:
                data = json.loads(payload)
                self.stats['messages_parsed'] += 1
                self._process_message(topic, data)
            except json.JSONDecodeError:
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
        # 格式1: 完整格式
        if 'device_id' in data and 'register' in data:
            device_id = data['device_id']
            register_name = data['register']
            value = float(data.get('value', 0))
            unit = data.get('unit', '')
            self._notify_callbacks(device_id, register_name, value, unit)
        
        # 格式3: 嵌套数据格式
        elif 'device_id' in data and 'data' in data:
            device_id = data['device_id']
            for register_name, value in data['data'].items():
                if isinstance(value, (int, float)):
                    self._notify_callbacks(device_id, register_name, float(value), '')
        
        # 格式2: 简单键值对
        else:
            device_id = topic.split('/')[-1] if '/' in topic else topic
            for register_name, value in data.items():
                if isinstance(value, (int, float)):
                    self._notify_callbacks(device_id, register_name, float(value), '')
    
    def _process_raw_message(self, topic: str, payload: str):
        """处理非JSON格式消息"""
        try:
            if '=' in payload:
                parts = payload.split('=')
                if len(parts) == 2:
                    device_id = topic.split('/')[-1] if '/' in topic else topic
                    register_name = parts[0].strip()
                    value = float(parts[1].strip())
                    self._notify_callbacks(device_id, register_name, value, '')
            else:
                value = float(payload.strip())
                device_id = topic.split('/')[-1] if '/' in topic else topic
                self._notify_callbacks(device_id, 'value', value, '')
        except (ValueError, IndexError):
            logger.debug(f"无法解析原始消息: {topic} = {payload}")
    
    def _notify_callbacks(self, device_id: str, register_name: str, 
                         value: float, unit: str):
        """
        通知所有回调函数
        签名统一: callback(device_id, register_name, value, unit)
        与OPCUA/REST客户端的DataCollector.on_data回调签名一致
        """
        for callback in self._data_callbacks:
            try:
                callback(device_id, register_name, value, unit)
            except Exception as e:
                logger.error(f"数据回调执行失败: {e}")


class MQTTDeviceManager:
    """
    MQTT设备管理器
    管理多个MQTT设备的数据采集
    """
    
    def __init__(self, config: dict):
        self.config = config
        self.client = None
        self._running = False
    
    def start(self, data_callback: Callable) -> bool:
        """启动MQTT数据采集"""
        try:
            self.client = MQTTClient(self.config)
            self.client.add_data_callback(data_callback)
            
            # 订阅主题
            topics = self.config.get('topics', [])
            for topic_config in topics:
                topic = topic_config.get('topic', '')
                qos = topic_config.get('qos', 1)
                if topic:
                    self.client.subscribe(topic, qos)
            
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
