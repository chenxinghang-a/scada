"""
增强版模拟客户端
================
使用设备行为模拟器生成更真实的工业数据

特性：
1. 物理模型驱动 - 参数之间有关联性
2. 状态机模拟 - 真实的设备运行状态
3. 故障注入 - 支持模拟各种故障场景
4. 数据连续性 - 确保数据流连续
5. 工业4.0兼容 - 生成的数据可直接用于OEE、SPC、预测性维护等
"""

import time
import struct
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .base_client import ModbusClientInterface, PushClientInterface
from .device_behavior_simulator import DeviceBehaviorSimulator, DeviceState, FaultType

logger = logging.getLogger(__name__)


class EnhancedSimulatedModbusClient(ModbusClientInterface):
    """
    增强版模拟Modbus客户端
    
    使用设备行为模拟器生成真实的工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = False
        
        # 创建设备行为模拟器
        self.behavior_simulator = DeviceBehaviorSimulator(
            device_id=config.get('id', 'unknown'),
            device_config=config
        )
        
        # 寄存器映射
        self._register_map = {}
        for reg in config.get('registers', []):
            self._register_map[reg['address']] = {
                'name': reg['name'],
                'data_type': reg.get('data_type', 'uint16'),
                'scale': reg.get('scale', 1.0),
                'offset': reg.get('offset', 0)
            }
        
        # 缓存最新数据
        self._latest_data: Dict[str, Any] = {}
        self._data_lock = threading.Lock()
        
        # 统计
        self.stats = {
            'total_reads': 0,
            'successful_reads': 0,
            'failed_reads': 0,
            'last_read_time': None,
            'last_error': None
        }
        
        logger.info(f"[增强模拟] Modbus客户端初始化: {config.get('name', 'unknown')}")
    
    def connect(self) -> bool:
        """连接设备"""
        self.connected = True
        self.behavior_simulator.start()
        
        # 启动数据更新线程
        self._update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self._update_thread.start()
        
        logger.info(f"[增强模拟] 设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接"""
        self.connected = False
        self.behavior_simulator.stop()
        logger.info(f"[增强模拟] 设备 {self.device_name} 已断开")
    
    def _update_loop(self):
        """数据更新循环"""
        while self.connected:
            try:
                # 更新行为模拟器
                data = self.behavior_simulator.update(1.0)
                
                # 更新缓存
                with self._data_lock:
                    self._latest_data = data
                
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"[增强模拟] 更新异常: {e}")
                time.sleep(1)
    
    def read_holding_registers(self, address: int, count: int,
                               slave_id: Optional[int] = None) -> Optional[List[int]]:
        """读取保持寄存器 — 支持写入值回读"""
        if not self.connected:
            return None
        
        # ===== 停机设备不返回数据 =====
        with self._data_lock:
            if self._latest_data.get('_stopped'):
                return None
        
        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        self.stats['last_read_time'] = time.time()
        
        # 检查是否有写入的值（用于回读验证）
        written_value = self.behavior_simulator.get_written_register_value(address)
        if written_value is not None and count == 1:
            return [written_value & 0xFFFF]
        
        # 获取寄存器配置
        reg_config = self._register_map.get(address)
        if not reg_config:
            # 未知寄存器，返回随机值
            if count >= 2:
                raw = struct.pack('>f', float(50 + 30 * time.time() % 100))
                return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
            return [100]
        
        # 从缓存获取数据
        with self._data_lock:
            value = self._latest_data.get(reg_config['name'], 0)
        
        # 应用缩放和偏移
        scale = reg_config.get('scale', 1.0)
        offset = reg_config.get('offset', 0)
        raw_value = (value - offset) / scale if scale != 0 else value
        
        # 根据数据类型编码
        data_type = reg_config.get('data_type', 'uint16')
        
        if data_type == 'float32':
            raw = struct.pack('>f', float(value))
            return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
        elif data_type in ('int32', 'uint32'):
            int_val = int(round(raw_value))
            raw = struct.pack('>i' if data_type == 'int32' else '>I', int_val)
            return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
        elif count >= 2:
            raw = struct.pack('>f', float(value))
            return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
        else:
            return [int(round(raw_value)) & 0xFFFF]
    
    def read_input_registers(self, address: int, count: int,
                             slave_id: Optional[int] = None) -> Optional[List[int]]:
        """读取输入寄存器（与保持寄存器逻辑相同）"""
        return self.read_holding_registers(address, count, slave_id)
    
    def read_coils(self, address: int, count: int,
                   slave_id: Optional[int] = None) -> Optional[List[bool]]:
        """读取线圈"""
        if not self.connected:
            return None
        
        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        
        # 根据设备状态返回
        state = self.behavior_simulator.state
        return [state == DeviceState.RUNNING for _ in range(count)]
    
    def read_discrete_inputs(self, address: int, count: int,
                             slave_id: Optional[int] = None) -> Optional[List[bool]]:
        """读取离散输入"""
        return self.read_coils(address, count, slave_id)
    
    def write_single_register(self, address: int, value: int,
                              slave_id: Optional[int] = None) -> bool:
        """写入单个寄存器 — 转发到行为模拟器影响模拟状态"""
        if not self.connected:
            return False
        
        logger.info(f"[增强模拟] 设备 {self.device_name} 写入寄存器: address={address}, value={value}")
        # 转发到行为模拟器，影响设备状态和物理参数
        self.behavior_simulator.handle_write_register(address, value)
        return True
    
    def write_single_coil(self, address: int, value: bool,
                          slave_id: Optional[int] = None) -> bool:
        """写入单个线圈 — 转发到行为模拟器影响模拟状态"""
        if not self.connected:
            return False
        
        logger.info(f"[增强模拟] 设备 {self.device_name} 写入线圈: address={address}, value={value}")
        # 转发到行为模拟器，影响设备状态
        self.behavior_simulator.handle_write_coil(address, value)
        return True
    
    def decode_float32(self, registers: List[int]) -> float:
        """解码float32"""
        raw = (registers[0] << 16) | registers[1]
        return struct.unpack('>f', struct.pack('>I', raw))[0]
    
    def decode_float64(self, registers: List[int]) -> float:
        """解码float64"""
        raw = struct.pack('>HHHH', registers[0], registers[1], registers[2], registers[3])
        return struct.unpack('>d', raw)[0]
    
    def decode_uint16(self, register: int) -> int:
        """解码uint16"""
        return register & 0xFFFF
    
    def decode_int16(self, register: int) -> int:
        """解码int16"""
        if register & 0x8000:
            return register - 0x10000
        return register
    
    def decode_int32(self, registers: List[int]) -> int:
        """解码int32"""
        raw = (registers[0] << 16) | registers[1]
        if raw & 0x80000000:
            raw -= 0x100000000
        return raw
    
    def decode_uint32(self, registers: List[int]) -> int:
        """解码uint32"""
        return (registers[0] << 16) | registers[1]
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'state': self.behavior_simulator.state.name,
            'health_score': self.behavior_simulator.health.overall_score,
            **self.stats
        }
    
    def get_latest_data(self) -> Dict[str, Any]:
        """获取最新数据"""
        with self._data_lock:
            # 停机设备返回空数据
            if self._latest_data.get('_stopped'):
                return {}
            return dict(self._latest_data)
    
    def inject_fault(self, fault_type: FaultType, severity: float = 0.5):
        """注入故障"""
        self.behavior_simulator.inject_fault(fault_type, severity)
    
    def force_state(self, state: DeviceState):
        """强制设置状态"""
        self.behavior_simulator.force_state(state)
    
    def inject_simulation_params(self, sim_params: dict):
        """注入模拟参数（由SimulationInitializer调用）"""
        self.behavior_simulator.inject_simulation_params(sim_params)


class EnhancedSimulatedOPCUAClient(PushClientInterface):
    """
    增强版模拟OPC UA客户端
    
    使用设备行为模拟器生成真实的工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = False
        
        # 创建设备行为模拟器
        self.behavior_simulator = DeviceBehaviorSimulator(
            device_id=config.get('id', 'unknown'),
            device_config=config
        )
        
        # 节点配置
        self.node_configs = config.get('nodes', [])
        
        # 缓存最新数据
        self.latest_data: Dict[str, Dict[str, Any]] = {}
        self._data_callbacks: List[Callable] = []
        self._push_thread: Optional[threading.Thread] = None
        self._running = False
        
        # 统计
        self.stats = {
            'connected_since': None,
            'nodes_subscribed': 0,
            'data_updates': 0,
            'errors': 0,
            'last_error': None
        }
        
        logger.info(f"[增强模拟] OPC UA客户端初始化: {config.get('name', 'unknown')}")
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调"""
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        """连接"""
        self.connected = True
        self._running = True
        self.stats['connected_since'] = datetime.now().isoformat()
        self.stats['nodes_subscribed'] = len(self.node_configs)
        
        # 启动行为模拟器
        self.behavior_simulator.start()
        
        # 启动数据推送线程
        self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread.start()
        
        logger.info(f"[增强模拟] OPC UA设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接"""
        self._running = False
        self.connected = False
        self.behavior_simulator.stop()
        logger.info(f"[增强模拟] OPC UA设备 {self.device_name} 已断开")
    
    def _push_loop(self):
        """数据推送循环"""
        while self._running and self.connected:
            try:
                self._generate_data()
                time.sleep(2)
            except Exception as e:
                logger.error(f"[增强模拟] OPC UA推送异常: {e}")
                time.sleep(1)
    
    def _generate_data(self):
        """生成数据"""
        # 更新行为模拟器
        data = self.behavior_simulator.update(2.0)
        
        # ===== 停机设备不产生数据 =====
        if data.get('_stopped'):
            return
        
        # 更新节点数据
        for i, node_cfg in enumerate(self.node_configs):
            name = node_cfg.get('name', node_cfg.get('node_id', 'unknown'))
            unit = node_cfg.get('unit', '')
            
            # 从行为模拟器获取数据
            value = data.get(name, 50 + 30 * time.time() % 100)
            
            self.latest_data[name] = {
                'value': round(float(value), 2),
                'unit': unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good',
                'node_id': node_cfg.get('node_id', '')
            }
            
            self.stats['data_updates'] += 1
            
            # 触发回调
            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, name, round(float(value), 2), unit)
                except Exception as e:
                    logger.error(f"[增强模拟] OPC UA回调异常: {e}")
    
    def get_latest_data(self) -> Dict[str, Dict[str, Any]]:
        """获取最新数据"""
        if self.connected:
            self._generate_data()
        # 停机设备返回空数据
        if self.behavior_simulator.state == DeviceState.STOPPED and not self.behavior_simulator._is_monitoring_device:
            return {}
        return dict(self.latest_data)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'state': self.behavior_simulator.state.name,
            'health_score': self.behavior_simulator.health.overall_score,
            **self.stats
        }
    
    def datachange_notification(self, node, val, data):
        pass
    
    def event_notification(self, event):
        pass
    
    def status_change_notification(self, status):
        pass


class EnhancedSimulatedMQTTClient(PushClientInterface):
    """
    增强版模拟MQTT客户端
    
    使用设备行为模拟器生成真实的工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = False
        
        # 创建设备行为模拟器
        self.behavior_simulator = DeviceBehaviorSimulator(
            device_id=config.get('id', 'unknown'),
            device_config=config
        )
        
        # 主题配置
        self.topics_config = self.config.get('topics', [])
        
        # 缓存最新数据
        self.latest_data: Dict[str, Dict[str, Any]] = {}
        self._data_callbacks: List[Callable] = []
        self._subscriptions: Dict[str, int] = {}
        self._push_thread: Optional[threading.Thread] = None
        self._running = False
        
        # 统计
        self.stats = {
            'messages_received': 0,
            'messages_parsed': 0,
            'parse_errors': 0,
            'connected_since': None,
            'last_message_time': None
        }
        
        logger.info(f"[增强模拟] MQTT客户端初始化: {config.get('name', 'unknown')}")
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调"""
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        """连接"""
        self.connected = True
        self._running = True
        self.stats['connected_since'] = datetime.now().isoformat()
        
        # 启动行为模拟器
        self.behavior_simulator.start()
        
        # 订阅主题
        for topic_cfg in self.topics_config:
            topic = topic_cfg.get('topic', '')
            if topic:
                self._subscriptions[topic] = topic_cfg.get('qos', 1)
        
        # 启动数据推送线程
        self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread.start()
        
        logger.info(f"[增强模拟] MQTT设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接"""
        self._running = False
        self.connected = False
        self.behavior_simulator.stop()
        logger.info(f"[增强模拟] MQTT设备 {self.device_name} 已断开")
    
    def subscribe(self, topic: str, qos: int = 1) -> bool:
        """订阅主题"""
        self._subscriptions[topic] = qos
        return True
    
    def unsubscribe(self, topic: str):
        """取消订阅"""
        self._subscriptions.pop(topic, None)
    
    def _push_loop(self):
        """数据推送循环"""
        while self._running and self.connected:
            try:
                self._generate_data()
                time.sleep(3)
            except Exception as e:
                logger.error(f"[增强模拟] MQTT推送异常: {e}")
                time.sleep(1)
    
    def _generate_data(self):
        """生成数据"""
        # 更新行为模拟器
        data = self.behavior_simulator.update(3.0)
        
        # ===== 停机设备不产生数据 =====
        if data.get('_stopped'):
            return
        
        # 更新主题数据
        for i, topic_cfg in enumerate(self.topics_config):
            name = topic_cfg.get('name', 'unknown')
            unit = topic_cfg.get('unit', '')
            
            # 从行为模拟器获取数据
            value = data.get(name, 50 + 30 * time.time() % 100)
            
            self.stats['messages_received'] += 1
            self.stats['messages_parsed'] += 1
            self.stats['last_message_time'] = datetime.now().isoformat()
            
            self.latest_data[name] = {
                'value': round(float(value), 2),
                'unit': unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good',
                'device_id': self.device_id
            }
            
            # 触发回调
            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, name, round(float(value), 2), unit)
                except Exception as e:
                    logger.error(f"[增强模拟] MQTT回调异常: {e}")
    
    def get_latest_data(self) -> Dict[str, Dict[str, Any]]:
        """获取最新数据"""
        # 停机设备返回空数据
        if self.behavior_simulator.state == DeviceState.STOPPED and not self.behavior_simulator._is_monitoring_device:
            return {}
        return dict(self.latest_data)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'state': self.behavior_simulator.state.name,
            'health_score': self.behavior_simulator.health.overall_score,
            **self.stats
        }
    
    def get_status(self) -> Dict[str, Any]:
        """获取状态"""
        return {
            'connected': self.connected,
            'broker': f"{self.config.get('host', 'localhost')}:{self.config.get('port', 1883)}",
            'subscriptions': list(self._subscriptions.keys()),
            'stats': self.stats.copy()
        }


class EnhancedSimulatedRESTClient(PushClientInterface):
    """
    增强版模拟REST客户端
    
    使用设备行为模拟器生成真实的工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = False
        
        # 创建设备行为模拟器
        self.behavior_simulator = DeviceBehaviorSimulator(
            device_id=config.get('id', 'unknown'),
            device_config=config
        )
        
        # 端点配置
        self.endpoints = config.get('endpoints', [])
        
        # 缓存最新数据
        self.latest_data: Dict[str, Dict[str, Any]] = {}
        self._data_callbacks: List[Callable] = []
        
        # 统计
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'connected_since': None,
            'last_request_time': None,
            'last_error': None
        }
        
        logger.info(f"[增强模拟] REST客户端初始化: {config.get('name', 'unknown')}")
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调"""
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        """连接"""
        self.connected = True
        self.stats['connected_since'] = datetime.now().isoformat()
        
        # 启动行为模拟器
        self.behavior_simulator.start()
        
        # 生成初始数据
        self._generate_data()
        
        logger.info(f"[增强模拟] REST设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接"""
        self.connected = False
        self.behavior_simulator.stop()
        logger.info(f"[增强模拟] REST设备 {self.device_name} 已断开")
    
    def get_latest_data(self) -> Dict[str, Dict[str, Any]]:
        """获取最新数据"""
        if self.connected:
            self._generate_data()
        # 停机设备返回空数据
        if self.behavior_simulator.state == DeviceState.STOPPED and not self.behavior_simulator._is_monitoring_device:
            return {}
        return dict(self.latest_data)
    
    def read_endpoint(self, endpoint_config: Dict[str, Any]) -> Any:
        """读取端点"""
        self.stats['total_requests'] += 1
        self.stats['successful_requests'] += 1
        self.stats['last_request_time'] = datetime.now().isoformat()
        
        name = endpoint_config.get('name', 'unknown')
        
        # 更新行为模拟器
        data = self.behavior_simulator.update(1.0)
        
        # 从行为模拟器获取数据
        value = data.get(name, 50 + 30 * time.time() % 100)
        
        return round(float(value), 2)
    
    def write_endpoint(self, endpoint_config: Dict[str, Any], value: Any) -> bool:
        """写入端点"""
        logger.info(f"[增强模拟] REST写入: {endpoint_config.get('name')} = {value}")
        return True
    
    def _generate_data(self):
        """生成数据"""
        # 更新行为模拟器
        data = self.behavior_simulator.update(1.0)
        
        # ===== 停机设备不产生数据 =====
        if data.get('_stopped'):
            return
        
        # 更新端点数据
        for i, ep in enumerate(self.endpoints):
            name = ep.get('name', 'unknown')
            unit = ep.get('unit', '')
            
            # 从行为模拟器获取数据
            value = data.get(name, 50 + 30 * time.time() % 100)
            
            self.latest_data[name] = {
                'value': round(float(value), 2),
                'unit': unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good',
                'endpoint': ep.get('path', ''),
                'device_id': self.device_id
            }
            
            # 触发回调
            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, name, round(float(value), 2), unit)
                except Exception as e:
                    logger.error(f"[增强模拟] REST回调异常: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'state': self.behavior_simulator.state.name,
            'health_score': self.behavior_simulator.health.overall_score,
            **self.stats
        }
