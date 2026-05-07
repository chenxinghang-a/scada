"""
模拟客户端模块
在没有真实设备时提供模拟数据

支持协议: Modbus TCP/RTU, OPC UA, MQTT, REST HTTP
simulation_mode=True时，所有协议都使用模拟客户端
"""

import math
import time
import random
import struct
import logging
import threading
from datetime import datetime
from typing import Dict, List, Any, Callable, Optional

logger = logging.getLogger(__name__)


# ==================== 模拟Modbus客户端 ====================

class SimulatedModbusClient:
    """
    模拟Modbus客户端
    生成仿真工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.device_id = config.get('id')
        self.device_name = config.get('name')
        self.connected = False
        self.start_time = time.time()
        
        self.stats = {
            'total_reads': 0, 'successful_reads': 0, 'failed_reads': 0,
            'last_read_time': None, 'last_error': None
        }
    
    def connect(self) -> bool:
        self.connected = True
        logger.info(f"[模拟] 设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        self.connected = False
        logger.info(f"[模拟] 设备 {self.device_name} 已断开")
    
    def read_holding_registers(self, address: int, count: int,
                               slave_id: int = None) -> Optional[List[int]]:
        if not self.connected:
            return None
        
        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        self.stats['last_read_time'] = time.time()
        
        t = time.time() - self.start_time
        
        # 根据设备ID中的关键字生成不同数据
        if 'temp' in self.device_id:
            base = 275 + 75 * math.sin(t / 60)
            noise = random.gauss(0, 3)
            value = base + noise
            if count == 2:
                raw = struct.pack('>f', value)
                return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
            return [int(value)]
        
        elif 'pressure' in self.device_id:
            base = 50 + 20 * math.sin(t / 45)
            noise = random.gauss(0, 1)
            value = base + noise
            if count == 2:
                raw = struct.pack('>f', value)
                return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
            return [int(value)]
        
        elif 'power' in self.device_id:
            if address == 0:  # 电压
                value = 2200 + random.gauss(0, 20)
            elif address == 2:  # 电流
                value = 5000 + 2000 * math.sin(t / 30) + random.gauss(0, 100)
            elif address == 4:  # 功率
                value = 10000 + 5000 * math.sin(t / 20) + random.gauss(0, 500)
            elif address == 6:  # 电量
                value = 1000 + t * 0.1
                if count == 4:
                    raw = struct.pack('>d', value)
                    return [struct.unpack('>H', raw[i:i+2])[0] for i in range(0, 8, 2)]
            else:
                value = random.randint(0, 1000)
            
            if count == 2:
                raw = struct.pack('>f', value)
                return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
            return [int(value)]
        
        return [random.randint(0, 1000)]
    
    def decode_float32(self, registers: List[int]) -> float:
        raw = (registers[0] << 16) | registers[1]
        return struct.unpack('>f', struct.pack('>I', raw))[0]
    
    def decode_float64(self, registers: List[int]) -> float:
        raw = struct.pack('>HHHH', registers[0], registers[1], registers[2], registers[3])
        return struct.unpack('>d', raw)[0]
    
    def decode_uint16(self, register: int) -> int:
        return register & 0xFFFF
    
    def decode_int16(self, register: int) -> int:
        if register & 0x8000:
            return register - 0x10000
        return register
    
    def write_single_register(self, address: int, value: int, slave_id: int = None) -> bool:
        if not self.connected:
            return False
        logger.info(f"[模拟] 设备 {self.device_name} 写入寄存器: address={address}, value={value}")
        return True
    
    def write_single_coil(self, address: int, value: bool, slave_id: int = None) -> bool:
        if not self.connected:
            return False
        logger.info(f"[模拟] 设备 {self.device_name} 写入线圈: address={address}, value={value}")
        return True
    
    def get_stats(self) -> Dict[str, Any]:
        return {'device_id': self.device_id, 'device_name': self.device_name,
                'connected': self.connected, **self.stats}


# ==================== 模拟OPC UA客户端 ====================

class SimulatedOPCUAClient:
    """
    模拟OPC UA客户端
    按节点配置生成仿真数据，后台线程持续推送（模拟真实OPC UA订阅）
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.device_id = config.get('id', 'opcua_sim')
        self.device_name = config.get('name', 'OPC UA模拟设备')
        self.connected = False
        self.start_time = time.time()
        
        self.node_configs = config.get('nodes', [])
        self.latest_data: Dict[str, Dict] = {}
        self._data_callbacks: List[Callable] = []
        self._push_thread: Optional[threading.Thread] = None
        self._running = False
        
        self.stats = {
            'connected_since': None, 'nodes_subscribed': 0,
            'data_updates': 0, 'errors': 0, 'last_error': None
        }
    
    def add_data_callback(self, callback: Callable):
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        self.connected = True
        self._running = True
        self.stats['connected_since'] = datetime.now().isoformat()
        self.stats['nodes_subscribed'] = len(self.node_configs)
        logger.info(f"[模拟] OPC UA设备 {self.device_name} 连接成功")
        
        # 生成初始数据
        self._generate_data()
        
        # 启动后台推送线程（模拟真实OPC UA订阅的持续数据变更）
        self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread.start()
        return True
    
    def disconnect(self):
        self._running = False
        self.connected = False
        logger.info(f"[模拟] OPC UA设备 {self.device_name} 已断开")
    
    def _push_loop(self):
        """后台线程：每2秒生成新数据并触发回调，模拟OPC UA订阅推送"""
        while self._running and self.connected:
            time.sleep(2)
            if self._running and self.connected:
                self._generate_data()
    
    def get_latest_data(self) -> Dict[str, Dict]:
        if self.connected:
            self._generate_data()
        return dict(self.latest_data)
    
    def get_stats(self) -> Dict[str, Any]:
        return {'device_id': self.device_id, 'device_name': self.device_name,
                'connected': self.connected, **self.stats}
    
    def _generate_data(self):
        """根据节点配置生成模拟数据"""
        t = time.time() - self.start_time
        
        for node_cfg in self.node_configs:
            name = node_cfg.get('name', node_cfg.get('node_id', 'unknown'))
            unit = node_cfg.get('unit', '')
            
            # 根据名称关键字生成数据
            if 'temp' in name.lower():
                value = 25.0 + 10 * math.sin(t / 60) + random.gauss(0, 0.5)
            elif 'pressure' in name.lower():
                value = 0.5 + 0.2 * math.sin(t / 45) + random.gauss(0, 0.01)
            elif 'speed' in name.lower():
                value = 1500 + 500 * math.sin(t / 30) + random.gauss(0, 10)
            elif 'status' in name.lower():
                value = 1 if int(t) % 10 < 8 else 0
            else:
                value = 50 + 30 * math.sin(t / 20) + random.gauss(0, 2)
            
            value = round(value, 2)
            self.latest_data[name] = {
                'value': value, 'unit': unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good', 'node_id': node_cfg.get('node_id', '')
            }
            
            self.stats['data_updates'] += 1
            
            # 触发回调
            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, name, value, unit)
                except Exception as e:
                    logger.error(f"[模拟] OPC UA回调异常: {e}")
    
    def datachange_notification(self, node, val, data):
        pass
    
    def event_notification(self, event):
        pass
    
    def status_change_notification(self, status):
        pass


# ==================== 模拟MQTT客户端 ====================

class SimulatedMQTTClient:
    """
    模拟MQTT客户端
    按topics配置生成仿真数据，后台线程持续推送（模拟真实MQTT消息到达）
    """
    
    def __init__(self, config: Dict[str, Any] = None, **kwargs):
        self.config = config or kwargs
        self.device_id = self.config.get('id', 'mqtt_sim')
        self.device_name = self.config.get('name', 'MQTT模拟设备')
        self.broker_host = self.config.get('host', 'localhost')
        self.broker_port = self.config.get('port', 1883)
        self.connected = False
        self.start_time = time.time()
        
        self.topics_config = self.config.get('topics', [])
        self.latest_data: Dict[str, Dict] = {}
        self._data_callbacks: List[Callable] = []
        self._subscriptions: Dict[str, int] = {}
        self._push_thread: Optional[threading.Thread] = None
        self._running = False
        
        self.stats = {
            'messages_received': 0, 'messages_parsed': 0,
            'parse_errors': 0, 'connected_since': None,
            'last_message_time': None
        }
    
    def add_data_callback(self, callback: Callable):
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        self.connected = True
        self._running = True
        self.stats['connected_since'] = datetime.now().isoformat()
        logger.info(f"[模拟] MQTT设备 {self.device_name} 连接成功")
        
        # 自动订阅topics
        for topic_cfg in self.topics_config:
            topic = topic_cfg.get('topic', '')
            if topic:
                self._subscriptions[topic] = topic_cfg.get('qos', 1)
        
        # 生成初始数据
        self._generate_data()
        
        # 后台线程持续推送，模拟真实MQTT消息到达
        self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread.start()
        return True
    
    def disconnect(self):
        self._running = False
        self.connected = False
        logger.info(f"[模拟] MQTT设备 {self.device_name} 已断开")
    
    def subscribe(self, topic: str, qos: int = 1) -> bool:
        self._subscriptions[topic] = qos
        return True
    
    def unsubscribe(self, topic: str):
        self._subscriptions.pop(topic, None)
    
    def get_latest_data(self) -> Dict[str, Dict]:
        return dict(self.latest_data)
    
    def get_stats(self) -> Dict[str, Any]:
        return {'device_id': self.device_id, 'device_name': self.device_name,
                'connected': self.connected, **self.stats}
    
    def get_status(self) -> Dict:
        return {
            'connected': self.connected,
            'broker': f'{self.broker_host}:{self.broker_port}',
            'subscriptions': list(self._subscriptions.keys()),
            'stats': self.stats.copy()
        }
    
    def _push_loop(self):
        """后台线程：每3秒生成新数据并触发回调，模拟MQTT消息到达"""
        while self._running and self.connected:
            time.sleep(3)
            if self._running and self.connected:
                self._generate_data()
    
    def _generate_data(self):
        """根据topics配置生成模拟数据，同时更新缓存和触发回调"""
        t = time.time() - self.start_time
        
        for topic_cfg in self.topics_config:
            name = topic_cfg.get('name', 'unknown')
            unit = topic_cfg.get('unit', '')
            
            if 'temp' in name.lower():
                value = 25.0 + 10 * math.sin(t / 60) + random.gauss(0, 0.5)
            elif 'humid' in name.lower():
                value = 60 + 20 * math.sin(t / 90) + random.gauss(0, 1)
            elif 'co2' in name.lower():
                value = 400 + 100 * math.sin(t / 120) + random.gauss(0, 5)
            else:
                value = 50 + 30 * math.sin(t / 20) + random.gauss(0, 2)
            
            value = round(value, 2)
            self.stats['messages_received'] += 1
            self.stats['messages_parsed'] += 1
            self.stats['last_message_time'] = datetime.now().isoformat()
            
            # 更新缓存（与OPC UA/REST保持接口一致）
            self.latest_data[name] = {
                'value': value, 'unit': unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good', 'device_id': self.device_id
            }
            
            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, name, value, unit)
                except Exception as e:
                    logger.error(f"[模拟] MQTT回调异常: {e}")


# ==================== 模拟REST客户端 ====================

class SimulatedRESTClient:
    """
    模拟REST HTTP客户端
    按endpoints配置生成仿真数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.device_id = config.get('id', 'rest_sim')
        self.device_name = config.get('name', 'REST模拟设备')
        self.base_url = config.get('base_url', 'http://localhost')
        self.connected = False
        self.start_time = time.time()
        
        self.endpoints = config.get('endpoints', [])
        self.latest_data: Dict[str, Dict] = {}
        self._data_callbacks: List[Callable] = []
        
        self.stats = {
            'total_requests': 0, 'successful_requests': 0,
            'failed_requests': 0, 'connected_since': None,
            'last_request_time': None, 'last_error': None
        }
    
    def add_data_callback(self, callback: Callable):
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        self.connected = True
        self.stats['connected_since'] = datetime.now().isoformat()
        logger.info(f"[模拟] REST设备 {self.device_name} 连接成功")
        
        # 生成初始数据
        self._generate_data()
        return True
    
    def disconnect(self):
        self.connected = False
        logger.info(f"[模拟] REST设备 {self.device_name} 已断开")
    
    def get_latest_data(self) -> Dict[str, Dict]:
        if self.connected:
            self._generate_data()
        return dict(self.latest_data)
    
    def read_endpoint(self, endpoint_config: Dict) -> Optional[Any]:
        """读取端点数据（模拟）"""
        self.stats['total_requests'] += 1
        self.stats['successful_requests'] += 1
        self.stats['last_request_time'] = datetime.now().isoformat()
        
        name = endpoint_config.get('name', 'unknown')
        t = time.time() - self.start_time
        
        if 'temp' in name.lower():
            return round(25.0 + 10 * math.sin(t / 60) + random.gauss(0, 0.5), 2)
        elif 'humid' in name.lower():
            return round(60 + 20 * math.sin(t / 90) + random.gauss(0, 1), 2)
        elif 'count' in name.lower() or 'production' in name.lower():
            return int(1000 + t * 0.5)
        else:
            return round(50 + 30 * math.sin(t / 20) + random.gauss(0, 2), 2)
    
    def write_endpoint(self, endpoint_config: Dict, value: Any) -> bool:
        """写入端点数据（模拟成功）"""
        logger.info(f"[模拟] REST写入: {endpoint_config.get('name')} = {value}")
        return True
    
    def get_stats(self) -> Dict[str, Any]:
        return {'device_id': self.device_id, 'device_name': self.device_name,
                'connected': self.connected, **self.stats}
    
    def _generate_data(self):
        """根据endpoints配置生成模拟数据"""
        t = time.time() - self.start_time
        
        for ep in self.endpoints:
            name = ep.get('name', 'unknown')
            unit = ep.get('unit', '')
            
            if 'temp' in name.lower():
                value = 25.0 + 10 * math.sin(t / 60) + random.gauss(0, 0.5)
            elif 'humid' in name.lower():
                value = 60 + 20 * math.sin(t / 90) + random.gauss(0, 1)
            elif 'count' in name.lower() or 'production' in name.lower():
                value = 1000 + t * 0.5
            else:
                value = 50 + 30 * math.sin(t / 20) + random.gauss(0, 2)
            
            value = round(value, 2)
            self.latest_data[name] = {
                'value': value, 'unit': unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good', 'endpoint': ep.get('path', ''),
                'device_id': self.device_id
            }
            
            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, name, value, unit)
                except Exception as e:
                    logger.error(f"[模拟] REST回调异常: {e}")
