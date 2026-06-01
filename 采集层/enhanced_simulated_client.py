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
import random
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .base_client import ModbusClientInterface, PushClientInterface
from .device_behavior_simulator import DeviceBehaviorSimulator, DeviceState, FaultType
from .simulated_client import is_device_stopped, _ESTOP_ACTIVE
from .modbus_client import ByteOrder

logger = logging.getLogger(__name__)


class EnhancedSimulatedModbusClient(ModbusClientInterface):
    """
    增强版模拟Modbus客户端
    
    使用设备行为模拟器生成真实的工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = False
        self.byte_order = ByteOrder(config.get('byte_order', 'ABCD').upper())

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

        # 每设备独立RNG（避免30台设备故障模式相关）
        self._rng = random.Random(hash(config.get('id', '')))

        # 通信故障模拟参数
        comm_cfg = config.get('communication', {})
        self._conn_fail_rate = comm_cfg.get('connect_fail_rate', 0.0)  # 默认不模拟连接失败
        self._latency_ms = comm_cfg.get('latency_ms', 0)  # 通信延迟
        self._packet_loss_rate = comm_cfg.get('packet_loss_rate', 0.0)  # 默认不模拟丢包
        self._random_disconnect_rate = comm_cfg.get('random_disconnect_rate', 0.0)  # 默认不模拟随机断线
        self._consecutive_failures = 0

        # 统计
        self.stats = {
            'total_reads': 0,
            'successful_reads': 0,
            'failed_reads': 0,
            'last_read_time': None,
            'last_error': None,
            'comm_failures': 0,
            'reconnects': 0
        }

        logger.info(f"[增强模拟] Modbus客户端初始化: {config.get('name', 'unknown')}")

    @staticmethod
    def _is_machinery(name: str) -> bool:
        """判断寄存器名是否属于机械类（停机时归零）"""
        import re
        _MACHINERY_KEYWORDS = ['motor_speed', 'conveyor_speed', 'pump_speed', 'fan_speed',
                               'spindle_speed', 'feed_rate', 'injection_speed', 'injection_force',
                               'clamping_force', 'shot_count', 'cycle_time', 'mixer']
        name_lower = name.lower()
        for kw in _MACHINERY_KEYWORDS:
            if re.search(r'(?<![a-z])' + re.escape(kw) + r'(?![a-z])', name_lower):
                return True
        return False

    def connect(self) -> bool:
        """连接设备（支持连接失败模拟）"""
        # 模拟连接失败
        if self._rng.random() < self._conn_fail_rate:
            self.stats['comm_failures'] += 1
            logger.warning(f"[增强模拟] 设备 {self.device_name} 连接失败（模拟通信故障）")
            return False

        # 防止重复connect()产生多个更新线程
        if hasattr(self, '_update_thread') and self._update_thread and self._update_thread.is_alive():
            self._running = False
            self._update_thread.join(timeout=2)

        self.connected = True
        self._running = True
        self._consecutive_failures = 0
        self.behavior_simulator.start()

        # 启动数据更新线程
        self._update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self._update_thread.start()

        logger.info(f"[增强模拟] 设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接（安全停止更新线程）"""
        self._running = False
        self.connected = False
        self.behavior_simulator.stop()
        if hasattr(self, '_update_thread') and self._update_thread and self._update_thread.is_alive():
            self._update_thread.join(timeout=3)
        logger.info(f"[增强模拟] 设备 {self.device_name} 已断开")

    def _update_loop(self):
        """数据更新循环"""
        last_update = time.time()
        while self._running and self.connected:
            try:
                now = time.time()
                dt = now - last_update
                last_update = now
                # 更新行为模拟器
                data = self.behavior_simulator.update(dt)

                # 更新缓存
                with self._data_lock:
                    self._latest_data = data

                time.sleep(1.0)
            except Exception as e:
                logger.error(f"[增强模拟] 更新异常: {e}")
                time.sleep(1)
    
    def read_holding_registers(self, address: int, count: int,
                               slave_id: Optional[int] = None) -> Optional[List[int]]:
        """读取保持寄存器 — 支持写入值回读和通信故障模拟"""
        if not self.connected:
            return None

        # 停机/E-STOP：机械归零，传感器保持环境值
        device_stopped = is_device_stopped(self.device_id)
        estop_active = _ESTOP_ACTIVE
        if device_stopped or estop_active:
            results = []
            for i in range(count):
                reg_addr = address + i
                reg_cfg = self._register_map.get(reg_addr, {})
                reg_name = reg_cfg.get('name', '')
                is_mach = self._is_machinery(reg_name)
                if device_stopped or (estop_active and is_mach):
                    results.append(0)
                else:
                    # 传感器：从缓存读取（行为模拟器仍在更新环境值）
                    val = self._latest_data.get(reg_name)
                    if val is None:
                        val = 25.0
                    scale = reg_cfg.get('scale', 1.0)
                    raw = val / scale if scale != 0 else val
                    dt = reg_cfg.get('data_type', 'uint16')
                    if dt == 'float32':
                        b = struct.pack('>f', float(raw))
                        results.extend([struct.unpack('>H', b[0:2])[0], struct.unpack('>H', b[2:4])[0]])
                    else:
                        results.append(int(round(raw)) & 0xFFFF)
            return results[:count]

        # 模拟通信延迟
        if self._latency_ms > 0:
            time.sleep(self._latency_ms / 1000.0)

        # 模拟丢包
        if self._rng.random() < self._packet_loss_rate:
            self.stats['total_reads'] += 1
            self.stats['failed_reads'] += 1
            self.stats['comm_failures'] += 1
            self._consecutive_failures += 1
            return None

        # 模拟随机断线（连续失败时概率上升）
        disconnect_prob = self._random_disconnect_rate * (1 + self._consecutive_failures)
        if self._rng.random() < disconnect_prob:
            self.connected = False
            self.stats['comm_failures'] += 1
            logger.warning(f"[增强模拟] 设备 {self.device_name} 随机断线")
            return None

        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        self.stats['last_read_time'] = time.time()
        self._consecutive_failures = 0
        
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
        
        # 从缓存获取数据（None保护：行为模拟器边界条件下可能返回None）
        with self._data_lock:
            value = self._latest_data.get(reg_config['name'])
            if value is None:
                value = 0

        # 应用缩放和偏移
        scale = reg_config.get('scale', 1.0)
        offset = reg_config.get('offset', 0) or 0
        raw_value = (value - offset) / scale if scale != 0 else value
        
        # 根据数据类型编码
        data_type = reg_config.get('data_type', 'uint16')
        
        if data_type == 'float32':
            raw = struct.pack('>f', float(raw_value))
            return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
        elif data_type in ('int32', 'uint32'):
            int_val = int(round(raw_value))
            raw = struct.pack('>i' if data_type == 'int32' else '>I', int_val)
            return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
        elif count >= 2:
            raw = struct.pack('>f', float(raw_value))
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
        """解码float32，支持4种字节序"""
        if len(registers) < 2:
            raise ValueError("需要至少2个寄存器")

        w1, w2 = registers[0], registers[1]

        if self.byte_order == ByteOrder.ABCD:
            raw = (w1 << 16) | w2
        elif self.byte_order == ByteOrder.BADC:
            # Byte-swap within each word, then combine
            w1_swapped = ((w1 & 0xFF) << 8) | ((w1 >> 8) & 0xFF)
            w2_swapped = ((w2 & 0xFF) << 8) | ((w2 >> 8) & 0xFF)
            raw = (w1_swapped << 16) | w2_swapped
        elif self.byte_order == ByteOrder.CDAB:
            # Word swap only
            raw = (w2 << 16) | w1
        elif self.byte_order == ByteOrder.DCBA:
            b0 = (w1 >> 8) & 0xFF
            b1 = w1 & 0xFF
            b2 = (w2 >> 8) & 0xFF
            b3 = w2 & 0xFF
            raw = (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
        else:
            raw = (w1 << 16) | w2

        return struct.unpack('!f', struct.pack('!I', raw))[0]
    
    def decode_float64(self, registers: List[int]) -> float:
        """解码float64，支持4种字节序"""
        if len(registers) < 4:
            raise ValueError("需要至少4个寄存器")

        if self.byte_order == ByteOrder.ABCD:
            raw = struct.pack('>HHHH', registers[0], registers[1], registers[2], registers[3])
        elif self.byte_order == ByteOrder.BADC:
            raw = struct.pack('>HHHH', registers[1], registers[0], registers[3], registers[2])
        elif self.byte_order == ByteOrder.CDAB:
            raw = struct.pack('>HHHH', registers[2], registers[3], registers[0], registers[1])
        elif self.byte_order == ByteOrder.DCBA:
            def _swap16(w):
                return ((w & 0xFF) << 8) | ((w >> 8) & 0xFF)
            b0 = _swap16(registers[0])
            b1 = _swap16(registers[1])
            b2 = _swap16(registers[2])
            b3 = _swap16(registers[3])
            raw = struct.pack('>HHHH', b3, b2, b1, b0)
        else:
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
            if is_device_stopped(self.device_id):
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
        self._data_lock = threading.RLock()

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
        with self._data_lock:
            self._do_generate_data()

    def _do_generate_data(self):
        """内部生成数据（需在锁内调用）"""
        # 停机设备不产生数据
        if is_device_stopped(self.device_id):
            return

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
        with self._data_lock:
            if self.connected:
                self._do_generate_data()
            # 停机设备返回空数据
            if self.behavior_simulator.state == DeviceState.STOPPED and not self.behavior_simulator.is_monitoring_device:
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
        if is_device_stopped(self.device_id):
            return

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
        if self.behavior_simulator.state == DeviceState.STOPPED and not self.behavior_simulator.is_monitoring_device:
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

    def _resolve_endpoint_address(self, name: str) -> int | None:
        """从端点名称反查寄存器地址（用于写操作反馈到行为模拟器）"""
        if not hasattr(self, 'behavior_simulator') or not self.behavior_simulator:
            return None
        for addr, reg_info in self.behavior_simulator._register_address_map.items():
            if reg_info.get('name') == name:
                return addr
        return None
    
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
        self._running = False
        self.connected = False
        self.behavior_simulator.stop()
        logger.info(f"[增强模拟] REST设备 {self.device_name} 已断开")
    
    def get_latest_data(self) -> Dict[str, Dict[str, Any]]:
        """获取最新数据"""
        if self.connected:
            self._generate_data()
        # 停机设备返回空数据
        if self.behavior_simulator.state == DeviceState.STOPPED and not self.behavior_simulator.is_monitoring_device:
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
    
    def write_endpoint(self, endpoint_config: Dict[str, Any], value: Any, method: str = 'PUT') -> bool:
        """
        写入端点（支持POST/PUT方法）

        Args:
            endpoint_config: 端点配置
            value: 写入值
            method: HTTP方法 (POST/PUT)

        Returns:
            是否写入成功
        """
        name = endpoint_config.get('name', 'unknown')
        path = endpoint_config.get('path', '/')

        # 模拟写入延迟
        time.sleep(0.05)

        # 模拟写入成功/失败（95%成功率）
        success = random.random() < 0.95

        if success:
            # 更新本地缓存
            self.latest_data[name] = {
                'value': value,
                'unit': endpoint_config.get('unit', ''),
                'timestamp': datetime.now().isoformat(),
                'quality': 'good',
                'endpoint': path,
                'device_id': self.device_id,
                'method': method
            }

            # 更新行为模拟器的内部状态（通过名称反查寄存器地址）
            if hasattr(self, 'behavior_simulator') and self.behavior_simulator:
                addr = self._resolve_endpoint_address(name)
                if addr is not None:
                    self.behavior_simulator.handle_write_register(addr, int(value) if isinstance(value, (int, float)) else 0)

            logger.info(f"[增强模拟] REST {method}写入成功: {path} -> {name} = {value}")
        else:
            logger.warning(f"[增强模拟] REST {method}写入失败: {path} -> {name} = {value}")

        self.stats['total_requests'] += 1
        if success:
            self.stats['successful_requests'] += 1
        else:
            self.stats['failed_requests'] += 1

        return success

    def post_endpoint(self, endpoint_config: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """
        POST请求到端点（创建/提交操作）

        Args:
            endpoint_config: 端点配置
            data: POST数据

        Returns:
            响应数据
        """
        name = endpoint_config.get('name', 'unknown')
        path = endpoint_config.get('path', '/')

        # 模拟POST处理延迟
        time.sleep(0.1)

        # 模拟POST成功/失败（90%成功率）
        success = random.random() < 0.90

        if success:
            # 生成响应数据
            response = {
                'success': True,
                'message': f'{name} 操作成功',
                'data': data,
                'timestamp': datetime.now().isoformat(),
                'request_id': f'req_{int(time.time() * 1000)}'
            }

            logger.info(f"[增强模拟] REST POST成功: {path} -> {name}")
        else:
            response = {
                'success': False,
                'message': f'{name} 操作失败',
                'error': 'SERVICE_UNAVAILABLE',
                'timestamp': datetime.now().isoformat()
            }

            logger.warning(f"[增强模拟] REST POST失败: {path} -> {name}")

        self.stats['total_requests'] += 1
        if success:
            self.stats['successful_requests'] += 1
        else:
            self.stats['failed_requests'] += 1

        return response

    def put_endpoint(self, endpoint_config: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """
        PUT请求到端点（更新操作）

        Args:
            endpoint_config: 端点配置
            data: PUT数据

        Returns:
            响应数据
        """
        name = endpoint_config.get('name', 'unknown')
        path = endpoint_config.get('path', '/')

        # 模拟PUT处理延迟
        time.sleep(0.08)

        # 模拟PUT成功/失败（95%成功率）
        success = random.random() < 0.95

        if success:
            # 更新本地缓存
            if 'value' in data:
                self.latest_data[name] = {
                    'value': data['value'],
                    'unit': endpoint_config.get('unit', ''),
                    'timestamp': datetime.now().isoformat(),
                    'quality': 'good',
                    'endpoint': path,
                    'device_id': self.device_id,
                    'method': 'PUT'
                }

            response = {
                'success': True,
                'message': f'{name} 更新成功',
                'data': data,
                'timestamp': datetime.now().isoformat()
            }

            logger.info(f"[增强模拟] REST PUT成功: {path} -> {name}")
        else:
            response = {
                'success': False,
                'message': f'{name} 更新失败',
                'error': 'CONFLICT',
                'timestamp': datetime.now().isoformat()
            }

            logger.warning(f"[增强模拟] REST PUT失败: {path} -> {name}")

        self.stats['total_requests'] += 1
        if success:
            self.stats['successful_requests'] += 1
        else:
            self.stats['failed_requests'] += 1

        return response

    def _generate_data(self):
        """生成数据"""
        if is_device_stopped(self.device_id):
            return

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
