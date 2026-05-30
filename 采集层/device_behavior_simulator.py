"""
设备行为模拟器
================
基于物理模型和状态机的真实工业设备模拟

核心特性：
1. 物理模型 - 设备参数之间的关联性（温度→压力→流量）
2. 设备状态机 - 运行/空闲/故障/维护/停机
3. 故障模拟 - 真实的故障场景和渐进式退化
4. 数据连续性 - 确保数据流连续，支持历史回放
5. 班次影响 - 白班/夜班对设备参数的影响

参考：
- ISA-95 设备状态模型
- OEE六大损失理论
- 预测性维护最佳实践
"""

import math
import time
import random
import logging
import threading
from enum import Enum
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field
from collections import deque

logger = logging.getLogger(__name__)


class DeviceState(Enum):
    """设备状态（ISA-95标准）"""
    STOPPED = 0      # 停机
    IDLE = 1         # 空闲/待机
    RUNNING = 2      # 运行
    FAULT = 3        # 故障
    MAINTENANCE = 4  # 维护中
    SETUP = 5        # 换型/调试


class FaultType(Enum):
    """故障类型"""
    NONE = "none"
    SENSOR_DRIFT = "sensor_drift"        # 传感器漂移
    OVERHEATING = "overheating"          # 过热
    PRESSURE_LEAK = "pressure_leak"      # 压力泄漏
    MOTOR_WEAR = "motor_wear"            # 电机磨损
    COMMUNICATION = "communication"      # 通信故障
    POWER_FLUCTUATION = "power_fluctuation"  # 电源波动


@dataclass
class DeviceHealth:
    """设备健康状态"""
    overall_score: float = 100.0        # 总体健康评分 0-100
    mechanical_health: float = 100.0    # 机械健康
    electrical_health: float = 100.0    # 电气健康
    thermal_health: float = 100.0       # 热健康
    vibration_health: float = 100.0     # 振动健康
    last_maintenance: datetime = field(default_factory=datetime.now)
    operating_hours: float = 0.0        # 累计运行小时
    cycle_count: int = 0                # 累计循环次数


@dataclass
class ProcessModel:
    """过程模型参数"""
    # 基础参数
    base_temperature: float = 50.0      # 基础温度
    base_pressure: float = 0.8          # 基础压力
    base_flow: float = 15.0             # 基础流量
    base_level: float = 60.0            # 基础液位
    
    # 关联系数（温度升高→压力升高→流量变化）
    temp_pressure_coeff: float = 0.005  # 温度对压力的影响系数
    temp_flow_coeff: float = 0.02       # 温度对流量的影响系数
    pressure_flow_coeff: float = 0.1    # 压力对流量的影响系数
    
    # 时间常数（模拟惯性）
    thermal_time_constant: float = 120.0    # 热惯性时间常数（秒）
    pressure_time_constant: float = 30.0    # 压力响应时间常数
    flow_time_constant: float = 15.0        # 流量响应时间常数

    # 电气/电机参数
    base_power_kw: float = 10.0             # 基础功率(kW)
    base_current: float = 20.0              # 基础电机电流(A)
    base_speed: float = 1500.0              # 基础电机转速(RPM)


class DeviceBehaviorSimulator:
    """
    设备行为模拟器
    
    模拟真实工业设备的行为，包括：
    - 物理参数关联
    - 状态转换
    - 故障注入
    - 班次影响
    - 写操作反馈（写入影响读取）
    - 设备特定输出（只生成该设备有的参数）
    """
    
    def __init__(self, device_id: str, device_config: Dict[str, Any]):
        """
        初始化设备行为模拟器
        
        Args:
            device_id: 设备ID
            device_config: 设备配置
        """
        self.device_id = device_id
        self.device_config = device_config
        self.device_name = device_config.get('name', device_id)
        
        # 设备状态
        self.state = DeviceState.IDLE
        self.state_start_time = time.time()
        self.state_history: List[Dict[str, Any]] = []
        
        # 设备健康
        self.health = DeviceHealth()
        
        # 过程模型
        self.process_model = ProcessModel()
        
        # 当前物理参数（带惯性）
        self._current_temp = self.process_model.base_temperature
        self._current_pressure = self.process_model.base_pressure
        self._current_flow = self.process_model.base_flow
        self._current_level = self.process_model.base_level

        # 电气/电机参数
        self._current_power_kw = self.process_model.base_power_kw
        self._motor_current = 0.0
        self._motor_speed = 0.0
        self._vibration = 0.0
        
        # 故障状态（支持多故障并发和级联）
        self.active_faults: Dict[FaultType, float] = {}  # fault -> severity (0-1)
        self.fault_timers: Dict[FaultType, float] = {}   # fault -> start time
        self._fault_cascade_rules: Dict[FaultType, List[tuple]] = {}
        self._setup_cascade_rules()

        # 兼容旧接口：单故障视图
        self.active_fault = FaultType.NONE
        self.fault_start_time = 0.0
        self.fault_severity = 0.0  # 0-1
        
        # 运行参数
        self.start_time = time.time()
        self._last_update = time.time()
        self._dt = 1.0  # default dt, updated each cycle
        self._running = True
        
        # ===== 设备角色：监测设备 vs 工作设备 =====
        # 监测设备（传感器/分析仪）不受停机命令影响，始终运行
        self._is_monitoring_device = self._detect_monitoring_device()
        
        # 数据回调
        self._data_callbacks: List[Callable] = []
        
        # ===== 写操作反馈：存储写入值，影响后续读取 =====
        self._written_values: Dict[int, Any] = {}  # address -> value
        self._written_coils: Dict[int, bool] = {}  # address -> bool
        
        # ===== 设备特定参数名集合（用于过滤输出） =====
        self._device_param_names: set = set()
        self._register_address_map: Dict[int, Dict[str, Any]] = {}  # address -> {name, data_type, scale}
        self._build_param_names()
        
        # 统计
        self.stats = {
            'total_cycles': 0,
            'fault_count': 0,
            'maintenance_count': 0,
            'operating_hours': 0.0,
            'last_state_change': datetime.now().isoformat()
        }
        
        # 根据设备类型配置过程模型
        self._configure_process_model()
        
        # 从预设simulation_params注入参数
        self._apply_simulation_params()
        
        device_role = "监测" if self._is_monitoring_device else "工作"
        logger.info(f"[行为模拟] 设备 {self.device_name} 初始化完成, 角色: {device_role}, 参数: {len(self._device_param_names)} 个")

    def _setup_cascade_rules(self):
        """设置故障级联规则

        真实工业场景：一个故障会引发连锁反应
        过热 → 电机磨损 → 传感器漂移
        压力泄漏 → 过热
        """
        self._fault_cascade_rules = {
            FaultType.OVERHEATING: [
                (0.5, FaultType.MOTOR_WEAR, 10),    # 过热50%时触发电机磨损（10秒延迟）
            ],
            FaultType.MOTOR_WEAR: [
                (0.3, FaultType.SENSOR_DRIFT, 15),  # 磨损30%导致传感器漂移（15秒延迟）
            ],
            FaultType.PRESSURE_LEAK: [
                (0.4, FaultType.OVERHEATING, 20),   # 压力泄漏40%导致过热（20秒延迟）
            ],
        }

    @property
    def is_monitoring_device(self) -> bool:
        """是否为监测设备（传感器/分析仪），监测设备不受停机命令影响"""
        return self._is_monitoring_device

    def _configure_process_model(self):
        """根据设备类型配置过程模型"""
        device_type = self.device_config.get('description', '').lower()
        
        # 锅炉类设备（base调高使running时偶尔触碰报警阈值180°C/1.2MPa）
        if '锅炉' in device_type or 'boiler' in device_type:
            self.process_model.base_temperature = 155.0
            self.process_model.base_pressure = 1.0
            self.process_model.thermal_time_constant = 180.0
            
        # 水处理设备
        elif '水处理' in device_type or 'water' in device_type:
            self.process_model.base_temperature = 25.0
            self.process_model.base_pressure = 0.3
            self.process_model.base_flow = 20.0
            
        # 注塑机
        elif '注塑' in device_type or 'injection' in device_type:
            self.process_model.base_temperature = 180.0
            self.process_model.base_pressure = 10.0
            
        # 电力分析仪
        elif '电力' in device_type or 'power' in device_type:
            self.process_model.base_temperature = 35.0
        
        # 化工/蒸馏（base调高使running时偶尔触碰报警阈值120°C）
        elif '化工' in device_type or '蒸馏' in device_type or 'distill' in device_type:
            self.process_model.base_temperature = 95.0
            self.process_model.base_pressure = 0.1
            
        # 涂装/喷涂
        elif '涂装' in device_type or '喷涂' in device_type or 'spray' in device_type:
            self.process_model.base_temperature = 160.0
            self.process_model.base_pressure = 0.4
            
        # 振动监测
        elif '振动' in device_type or 'vibration' in device_type:
            self.process_model.base_temperature = 45.0
            
        # 水质监测
        elif '水质' in device_type or 'quality' in device_type:
            self.process_model.base_temperature = 22.0
            self.process_model.base_flow = 48.0
    
    def _build_param_names(self):
        """从设备配置中提取该设备拥有的参数名集合"""
        # Modbus寄存器
        for reg in self.device_config.get('registers', []):
            name = reg.get('name', '')
            if name:
                self._device_param_names.add(name)
                addr = reg.get('address')
                if addr is not None:
                    self._register_address_map[addr] = {
                        'name': name,
                        'data_type': reg.get('data_type', 'uint16'),
                        'scale': reg.get('scale', 1.0),
                    }
        
        # OPC UA节点
        for node in self.device_config.get('nodes', []):
            name = node.get('name', '')
            if name:
                self._device_param_names.add(name)
        
        # MQTT主题
        for topic in self.device_config.get('topics', []):
            name = topic.get('name', '')
            if name:
                self._device_param_names.add(name)
        
        # REST端点
        for ep in self.device_config.get('endpoints', []):
            name = ep.get('name', '')
            if name:
                self._device_param_names.add(name)
    
    def _apply_simulation_params(self):
        """从设备配置中的simulation_params注入参数到过程模型"""
        # simulation_params通常在SimulationInitializer中设置，
        # 但也可以从device_config中直接读取
        sim_params = self.device_config.get('_simulation_params', {})
        if not sim_params:
            return
        
        base_values = sim_params.get('base_values', {})
        
        # 用base_values配置过程模型的基础参数
        for name, value in base_values.items():
            name_lower = name.lower()
            if 'temperature' in name_lower or 'temp' in name_lower:
                # 取第一个温度参数作为基础温度
                if abs(value - self.process_model.base_temperature) > 20:
                    self.process_model.base_temperature = float(value)
                    self._current_temp = float(value)
            elif 'pressure' in name_lower and 'spray' not in name_lower:
                if abs(value - self.process_model.base_pressure) > 0.1:
                    self.process_model.base_pressure = float(value)
                    self._current_pressure = float(value)
            elif 'flow' in name_lower:
                if abs(value - self.process_model.base_flow) > 2:
                    self.process_model.base_flow = float(value)
                    self._current_flow = float(value)
            elif 'level' in name_lower:
                level_pct = float(value) / 300 * 100 if float(value) > 100 else float(value)
                self.process_model.base_level = level_pct
                self._current_level = level_pct
    
    def _detect_monitoring_device(self) -> bool:
        """检测设备是否为监测/检测设备（不受停机命令影响）"""
        desc = self.device_config.get('description', '').lower()
        name = self.device_config.get('name', '').lower()
        device_id = self.device_config.get('id', '').lower()
        
        monitoring_keywords = [
            '监测', '监控', '传感器', '分析仪', '检测', '仪表',
            'sensor', 'monitor', 'analyzer', 'meter', 'gauge',
            '振动', 'vibration', '水质', 'water_quality',
            '电力分析', 'power_analy', 'iolink', 'io-link',
            '信号灯', 'signal_tower', '继电器', 'relay',
            '指示灯', 'alarm', '报警'
        ]
        
        combined = f"{desc} {name} {device_id}"
        return any(kw in combined for kw in monitoring_keywords)
    
    def inject_simulation_params(self, sim_params: Dict[str, Any]):
        """外部注入模拟参数（由SimulationInitializer调用）"""
        self.device_config['_simulation_params'] = sim_params
        self._apply_simulation_params()
        logger.info(f"[行为模拟] 设备 {self.device_name} 已注入模拟参数")
    
    def handle_write_register(self, address: int, value: int):
        """
        处理写寄存器命令 — 写操作影响后续读取
        
        核心逻辑：
        1. 存储写入值（回读验证用）
        2. 识别控制命令（启动/停止/复位）
        3. 影响设备状态和物理参数
        """
        self._written_values[address] = value
        
        # 查找该地址对应的寄存器名
        reg_info = self._register_address_map.get(address)
        reg_name = reg_info['name'] if reg_info else ''
        
        # ===== 控制命令识别 =====
        # 通用约定：地址100 = 启动/停止控制
        if address == 100:
            # 监测设备不受停机命令影响
            if self._is_monitoring_device and value == 0:
                logger.info(f"[行为模拟] 监测设备 {self.device_name} 忽略停机命令")
                return
            
            if value == 1:
                # 启动命令
                if self.state in (DeviceState.STOPPED, DeviceState.IDLE):
                    self._change_state(DeviceState.RUNNING)
                    logger.info(f"[行为模拟] 设备 {self.device_name} 收到启动命令，状态→RUNNING")
            elif value == 0:
                # 停止命令
                if self.state in (DeviceState.RUNNING, DeviceState.FAULT):
                    self._change_state(DeviceState.STOPPED)
                    logger.info(f"[行为模拟] 设备 {self.device_name} 收到停止命令，状态→STOPPED")
            return
        
        # 通用约定：地址101 = 复位命令
        if address == 101:
            if value == 1:
                self.active_faults.clear()
                self.fault_timers.clear()
                self.active_fault = FaultType.NONE
                self.fault_severity = 0
                self._change_state(DeviceState.IDLE)
                logger.info(f"[行为模拟] 设备 {self.device_name} 收到复位命令，清除所有故障，状态→IDLE")
            return
        
        # ===== 按寄存器名影响物理参数 =====
        if reg_name:
            name_lower = reg_name.lower()
            
            # 温度设定点
            if 'setpoint' in name_lower and 'temp' in name_lower:
                self.process_model.base_temperature = float(value)
                logger.info(f"[行为模拟] 设备 {self.device_name} 温度设定→{value}°C")
            
            # 压力设定点
            elif 'setpoint' in name_lower and 'pressure' in name_lower:
                self.process_model.base_pressure = float(value)
                logger.info(f"[行为模拟] 设备 {self.device_name} 压力设定→{value}")
            
            # 流量设定点
            elif 'setpoint' in name_lower and 'flow' in name_lower:
                self.process_model.base_flow = float(value)
                logger.info(f"[行为模拟] 设备 {self.device_name} 流量设定→{value}")
            
            # 加热器开关
            elif 'heater' in name_lower and ('enable' in name_lower or 'switch' in name_lower):
                if value == 0:
                    # 关闭加热→温度下降
                    self.process_model.base_temperature = max(20, self.process_model.base_temperature - 30)
                    logger.info(f"[行为模拟] 设备 {self.device_name} 加热关闭")
            
            # 泵开关
            elif 'pump' in name_lower and ('enable' in name_lower or 'switch' in name_lower):
                if value == 0:
                    # 关闭泵→流量归零
                    self.process_model.base_flow = 0
                    logger.info(f"[行为模拟] 设备 {self.device_name} 泵关闭")
                else:
                    self.process_model.base_flow = 15.0  # 恢复默认流量
            
            # 阀门开度
            elif 'valve' in name_lower:
                # 阀门开度影响流量
                self.process_model.base_flow = 15.0 * (float(value) / 100)
                logger.info(f"[行为模拟] 设备 {self.device_name} 阀门开度→{value}%")
            
            # 继电器写入
            elif 'relay' in name_lower:
                # 继电器状态直接存储，不影响物理参数
                pass
            
            # 信号灯写入
            elif 'light' in name_lower or 'buzzer' in name_lower:
                # 信号灯直接存储
                pass
    
    def handle_write_coil(self, address: int, value: bool):
        """
        处理写线圈命令 — 线圈通常是开关量
        
        通用约定：
        - address 0 = 启动/停止（True=启动, False=停止）
        - address 1 = 复位
        """
        self._written_coils[address] = value
        
        # 通用约定：线圈0 = 启动/停止
        if address == 0:
            # 监测设备不受停机命令影响
            if self._is_monitoring_device and not value:
                logger.info(f"[行为模拟] 监测设备 {self.device_name} 忽略线圈停机命令")
                return
            
            if value:
                if self.state in (DeviceState.STOPPED, DeviceState.IDLE):
                    self._change_state(DeviceState.RUNNING)
                    logger.info(f"[行为模拟] 设备 {self.device_name} 线圈启动，状态→RUNNING")
            else:
                if self.state in (DeviceState.RUNNING, DeviceState.FAULT):
                    self._change_state(DeviceState.STOPPED)
                    logger.info(f"[行为模拟] 设备 {self.device_name} 线圈停止，状态→STOPPED")
            return
        
        # 线圈1 = 复位
        if address == 1 and value:
            self.active_faults.clear()
            self.fault_timers.clear()
            self.active_fault = FaultType.NONE
            self.fault_severity = 0
            self._change_state(DeviceState.IDLE)
            logger.info(f"[行为模拟] 设备 {self.device_name} 线圈复位，清除所有故障，状态→IDLE")
    
    def get_written_register_value(self, address: int) -> Optional[int]:
        """获取写入的寄存器值（用于回读验证）"""
        return self._written_values.get(address)
    
    def get_written_coil_value(self, address: int) -> Optional[bool]:
        """获取写入的线圈值"""
        return self._written_coils.get(address)
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调"""
        self._data_callbacks.append(callback)
    
    def start(self):
        """启动模拟器"""
        self._running = True
        self.state = DeviceState.RUNNING
        self.state_start_time = time.time()
        logger.info(f"[行为模拟] 设备 {self.device_name} 开始运行")
    
    def stop(self):
        """停止模拟器"""
        self._running = False
        self.state = DeviceState.STOPPED
        logger.info(f"[行为模拟] 设备 {self.device_name} 已停止")
    
    def update(self, dt: float) -> Dict[str, Any]:
        """
        更新设备状态（每个周期调用）
        
        Args:
            dt: 时间增量（秒）
            
        Returns:
            当前所有参数值
        """
        if not self._running:
            return {}

        current_time = time.time()
        self._dt = dt  # store for fault probability calculations
        
        # 1. 更新设备健康（退化）
        self._update_health(dt)
        
        # 2. 检查是否触发故障
        self._check_fault_triggers()
        
        # 2.5 配方驱动 - 应用配方设定值到过程模型
        self._apply_recipe_setpoints(dt)

        # 3. 更新物理参数（带惯性）
        self._update_process_variables(dt)

        # 3.5 应用所有活跃故障效果（多故障并发 + 级联）
        self._apply_fault_effects()

        # 4. 更新状态机
        self._update_state_machine(dt)
        
        # 5. 更新统计
        self._update_stats(dt)
        
        # 6. 生成输出数据
        data = self._generate_output_data()
        
        # 7. 触发回调
        for callback in self._data_callbacks:
            try:
                callback(self.device_id, data)
            except Exception as e:
                logger.error(f"[行为模拟] 回调异常: {e}")
        
        self._last_update = current_time
        return data
    
    def _apply_recipe_setpoints(self, dt: float):
        """应用配方设定值到过程模型"""
        if not hasattr(self, 'recipe_simulator') or not self.recipe_simulator:
            return
        if not self.recipe_simulator.is_running:
            return

        setpoints = self.recipe_simulator.update(dt)
        if not setpoints:
            return

        # 将配方设定值写入过程模型基础参数
        for param, value in setpoints.items():
            name_lower = param.lower()
            if 'temperature' in name_lower or 'temp' in name_lower:
                self.process_model.base_temperature = float(value)
            elif 'pressure' in name_lower:
                self.process_model.base_pressure = float(value) / 100  # 归一化
            elif 'flow' in name_lower:
                self.process_model.base_flow = float(value)
            elif 'speed' in name_lower:
                self.process_model.base_speed = float(value)

    def _update_health(self, dt: float):
        """更新设备健康状态（渐进式退化 + 多故障叠加惩罚）"""
        if self.state == DeviceState.RUNNING:
            # 运行时健康退化
            hours = dt / 3600.0
            self.health.operating_hours += hours

            # 机械磨损（缓慢）
            self.health.mechanical_health -= 0.001 * hours

            # 电气退化
            self.health.electrical_health -= 0.0005 * hours

            # 热退化（与温度相关）
            temp_factor = max(0, (self._current_temp - 80) / 100)
            self.health.thermal_health -= 0.002 * temp_factor * hours

            # 振动退化
            self.health.vibration_health -= 0.0008 * hours

            # 计算总体健康
            self.health.overall_score = (
                self.health.mechanical_health * 0.3 +
                self.health.electrical_health * 0.25 +
                self.health.thermal_health * 0.25 +
                self.health.vibration_health * 0.2
            )

            # 多故障叠加惩罚
            fault_penalty = 0
            for fault_type, severity in self.active_faults.items():
                penalty = {
                    FaultType.SENSOR_DRIFT: 10,
                    FaultType.OVERHEATING: 30,
                    FaultType.PRESSURE_LEAK: 20,
                    FaultType.MOTOR_WEAR: 25,
                    FaultType.COMMUNICATION: 15,
                    FaultType.POWER_FLUCTUATION: 20,
                }.get(fault_type, 10)
                fault_penalty += penalty * severity

            self.health.overall_score -= fault_penalty

            # 温度异常扣分
            if self._current_temp > 200:
                self.health.overall_score -= (self._current_temp - 200) * 0.5

            # 振动异常扣分
            if self._vibration > 5:
                self.health.overall_score -= (self._vibration - 5) * 2

            # 限制在0-100
            self.health.overall_score = max(0, min(100, self.health.overall_score))
            
        elif self.state == DeviceState.MAINTENANCE:
            # 维护时恢复健康
            self.health.mechanical_health = min(100, self.health.mechanical_health + 0.1)
            self.health.electrical_health = min(100, self.health.electrical_health + 0.1)
            self.health.thermal_health = min(100, self.health.thermal_health + 0.1)
            self.health.vibration_health = min(100, self.health.vibration_health + 0.1)
    
    def _check_fault_triggers(self):
        """检查是否触发新故障（允许并发，多种故障独立检查）"""
        health = self.health.overall_score

        # 健康度过低直接触发故障（不检查已有故障）
        if health < 30 and FaultType.MOTOR_WEAR not in self.active_faults:
            self._trigger_fault(FaultType.MOTOR_WEAR, severity=0.8)

        # 温度过高触发过热故障
        if self._current_temp > 150 and FaultType.OVERHEATING not in self.active_faults:
            temp_prob = 0.01 * math.exp((100 - health) / 30)
            if random.random() < temp_prob:
                self._trigger_fault(FaultType.OVERHEATING, severity=0.6)

        # 每种故障独立概率检查（不再互斥）
        fault_checks = [
            (FaultType.SENSOR_DRIFT, 0.001, 70),
            (FaultType.OVERHEATING, 0.0005, 50),
            (FaultType.PRESSURE_LEAK, 0.0008, 60),
            (FaultType.MOTOR_WEAR, 0.0003, 40),
            (FaultType.COMMUNICATION, 0.001, 80),
            (FaultType.POWER_FLUCTUATION, 0.0005, 75),
        ]

        for fault_type, base_prob, health_threshold in fault_checks:
            if fault_type in self.active_faults:
                continue  # 已有此故障，跳过

            if health < health_threshold:
                fault_prob = base_prob * math.exp((health_threshold - health) / 20)
                if random.random() < fault_prob * self._dt:
                    self._trigger_fault(fault_type, severity=0.1)
    
    def _trigger_fault(self, fault_type: FaultType, severity: float = 0.5):
        """触发故障（支持多故障并发）"""
        if fault_type == FaultType.NONE:
            return

        self.active_faults[fault_type] = min(1.0, severity)
        self.fault_timers[fault_type] = time.time()

        # 兼容旧接口：维护单故障视图
        self.active_fault = fault_type
        self.fault_start_time = time.time()
        self.fault_severity = severity

        self.state = DeviceState.FAULT
        self.state_start_time = time.time()
        self.stats['fault_count'] += 1

        logger.warning(f"[行为模拟] 设备 {self.device_name} 触发故障: {fault_type.value} "
                        f"(严重度: {severity:.2f}, 当前活跃故障数: {len(self.active_faults)})")
    
    def _update_process_variables(self, dt: float):
        """更新物理参数 - Antoine方程、质量平衡、电机特性"""
        t = time.time() - self.start_time

        # 班次影响（24小时周期）
        hour_of_day = (t / 3600) % 24
        shift_factor = 1.0 + 0.05 * math.sin((hour_of_day - 6) * math.pi / 12)

        # ===== 温度 - 一阶惯性 =====
        target_temp = self.process_model.base_temperature * shift_factor
        if self.state == DeviceState.RUNNING:
            target_temp += 20 * math.sin(t / 300)
        elif self.state == DeviceState.IDLE:
            target_temp = self.process_model.base_temperature - 10
        elif self.state == DeviceState.FAULT:
            # 故障效果由 _apply_fault_effects 统一处理
            pass
        else:
            target_temp = self.process_model.base_temperature - 30

        alpha_temp = 1 - math.exp(-dt / self.process_model.thermal_time_constant)
        self._current_temp += (target_temp - self._current_temp) * alpha_temp
        self._current_temp += random.gauss(0, 0.3)

        # 偶发过程扰动
        if random.random() < 0.001 and self.state == DeviceState.RUNNING:
            spike = random.uniform(10, 30)
            self._current_temp += spike
            logger.debug(f"[扰动] {self.device_name} 温度尖峰 +{spike:.1f}°C")

        # ===== 压力 - Antoine方程近似（饱和蒸汽压） =====
        # P(MPa) ~ 0.00001 * exp(0.05 * T) for 20-200°C range
        if self._current_temp > 0:
            target_pressure = 0.00001 * math.exp(0.05 * self._current_temp)
        else:
            target_pressure = 0.1

        # 故障对压力的影响由 _apply_fault_effects 统一处理

        alpha_pressure = 1 - math.exp(-dt / self.process_model.pressure_time_constant)
        self._current_pressure += (target_pressure - self._current_pressure) * alpha_pressure
        self._current_pressure += random.gauss(0, 0.005)
        self._current_pressure = max(0.01, self._current_pressure)

        # ===== 流量 - Q ∝ sqrt(ΔP) * f(T) =====
        pressure_factor = math.sqrt(max(0, self._current_pressure - 0.1))
        temp_factor = 1 + 0.01 * (self._current_temp - self.process_model.base_temperature)
        state_flow_factor = 1.0 if self.state == DeviceState.RUNNING else 0.1
        if self.state == DeviceState.STOPPED:
            state_flow_factor = 0.0
        target_flow = self.process_model.base_flow * pressure_factor * temp_factor * state_flow_factor

        alpha_flow = 1 - math.exp(-dt / self.process_model.flow_time_constant)
        self._current_flow += (target_flow - self._current_flow) * alpha_flow
        self._current_flow += random.gauss(0, 0.1)
        self._current_flow = max(0, self._current_flow)

        # ===== 液位 - 质量平衡（流入-流出积分） =====
        flow_balance = self._current_flow - self.process_model.base_flow
        self._current_level += flow_balance * 0.001 * dt
        self._current_level = max(0, min(100, self._current_level))
        self._current_level += random.gauss(0, 0.1)

        # ===== 电机参数关联 =====
        if self.state == DeviceState.RUNNING:
            # 功率跟踪过程负载
            self._current_power_kw = self.process_model.base_power_kw * \
                (self._current_flow / max(0.1, self.process_model.base_flow)) * \
                (1 + random.gauss(0, 0.05))

            # 电机电流 ∝ 负载扭矩 ∝ 流量
            load_ratio = self._current_flow / max(0.1, self.process_model.base_flow)
            self._motor_current = self.process_model.base_current * load_ratio * \
                                  (1 + random.gauss(0, 0.05))
            # 电机速度略降（滑差特性）
            self._motor_speed = self.process_model.base_speed * (1 - 0.03 * (load_ratio - 1))
        else:
            self._current_power_kw = random.gauss(0, 0.2)
            self._motor_current = random.gauss(0, 0.1)
            self._motor_speed = 0

        # 振动 - 与速度和负载相关
        if self.state == DeviceState.RUNNING:
            self._vibration = 1.5 + 0.5 * (self._motor_current / max(0.1, self.process_model.base_current)) + \
                              random.gauss(0, 0.2)
        else:
            self._vibration = random.gauss(0, 0.05)

        # 故障影响由 _apply_fault_effects 统一处理

    def _apply_fault_effects(self):
        """应用所有活跃故障的效果（多故障并发 + 级联扩散）"""
        if not self.active_faults:
            return

        for fault_type, severity in list(self.active_faults.items()):
            if fault_type == FaultType.SENSOR_DRIFT:
                t = time.time() - self.start_time
                self._current_temp += 5 * severity * math.sin(t / 10)

            elif fault_type == FaultType.OVERHEATING:
                self._current_temp += 50 * severity
                # 级联检查：过热导致电机磨损
                self._check_cascade(fault_type, severity)

            elif fault_type == FaultType.PRESSURE_LEAK:
                self._current_pressure *= (1 - 0.3 * severity)
                self._current_flow += 2 * severity  # 泄漏增加流量
                # 级联检查：压力泄漏导致过热
                self._check_cascade(fault_type, severity)

            elif fault_type == FaultType.MOTOR_WEAR:
                self._motor_current *= (1 + 0.2 * severity)  # 磨损增加电流
                self._vibration += 3 * severity  # 磨损增加振动
                # 级联检查：电机磨损导致传感器漂移
                self._check_cascade(fault_type, severity)

            elif fault_type == FaultType.COMMUNICATION:
                # 通信故障不影响物理参数，但影响数据质量
                pass

            elif fault_type == FaultType.POWER_FLUCTUATION:
                voltage_factor = 1 + 0.1 * severity * math.sin(time.time() * 5)
                self._current_power_kw *= voltage_factor

        # 故障严重度随时间增长
        for fault_type in list(self.active_faults.keys()):
            growth_rate = 0.01 if fault_type != FaultType.COMMUNICATION else -0.05
            self.active_faults[fault_type] = min(1.0,
                self.active_faults[fault_type] + growth_rate * self._dt)

            # 通信故障会自行恢复（间歇性）
            if fault_type == FaultType.COMMUNICATION and random.random() < 0.01:
                del self.active_faults[fault_type]
                del self.fault_timers[fault_type]
                logger.info(f"[行为模拟] 设备 {self.device_name} 通信故障自行恢复")

        # 更新兼容旧接口：取最严重的故障作为主视图
        if self.active_faults:
            worst_fault = max(self.active_faults, key=self.active_faults.get)
            self.active_fault = worst_fault
            self.fault_severity = self.active_faults[worst_fault]
            self.fault_start_time = self.fault_timers.get(worst_fault, time.time())
        else:
            self.active_fault = FaultType.NONE
            self.fault_severity = 0
            # 无活跃故障时恢复正常状态
            if self.state == DeviceState.FAULT:
                self._change_state(DeviceState.RUNNING)

    def _check_cascade(self, parent_fault: FaultType, severity: float):
        """检查是否触发级联故障"""
        rules = self._fault_cascade_rules.get(parent_fault, [])
        for trigger_severity, new_fault, delay in rules:
            if severity >= trigger_severity and new_fault not in self.active_faults:
                # 检查延迟：从父故障触发开始计算
                elapsed = time.time() - self.fault_timers.get(parent_fault, time.time())
                if elapsed >= delay:
                    self._trigger_fault(new_fault, severity=0.1)
                    logger.warning(f"[行为模拟] 设备 {self.device_name} 级联故障: "
                                    f"{parent_fault.value} → {new_fault.value}")

    def _update_state_machine(self, dt: float):
        """更新状态机
        
        优化：状态转换更合理
        - RUNNING: 正常运行，健康度过低→维护
        - IDLE: 待机，可被启动命令唤醒
        - STOPPED: 停机（由写操作触发），只能被启动命令唤醒
        - FAULT: 故障，超时后自动恢复或进入维护
        - MAINTENANCE: 维护，完成后恢复
        """
        state_duration = time.time() - self.state_start_time
        
        if self.state == DeviceState.RUNNING:
            # 运行时健康度过低→维护
            if self.health.overall_score < 20:
                self._change_state(DeviceState.MAINTENANCE)
            
            # 运行很长时间后小概率转为空闲（模拟间歇性停机）
            elif state_duration > 1800 and random.random() < 0.002:  # 30分钟后0.2%概率
                self._change_state(DeviceState.IDLE)
        
        elif self.state == DeviceState.IDLE:
            # 空闲一段时间后自动恢复运行（模拟自动调度）
            if state_duration > 120 and random.random() < 0.02:  # 2分钟后2%概率
                self._change_state(DeviceState.RUNNING)
        
        elif self.state == DeviceState.STOPPED:
            # STOPPED状态只能由外部启动命令唤醒，不自动恢复
            # 这确保了写操作的确定性
            pass
        
        elif self.state == DeviceState.FAULT:
            # 故障一段时间后尝试恢复或进入维护
            if state_duration > 300:  # 5分钟后
                if random.random() < 0.4:  # 40%概率自动恢复
                    self.active_faults.clear()
                    self.fault_timers.clear()
                    self.active_fault = FaultType.NONE
                    self.fault_severity = 0
                    self._change_state(DeviceState.RUNNING)
                else:
                    self._change_state(DeviceState.MAINTENANCE)
        
        elif self.state == DeviceState.MAINTENANCE:
            # 维护完成后恢复
            if state_duration > 600:  # 10分钟维护时间（原30分钟）
                self.health.overall_score = 90  # 维护后恢复到90%
                self.health.mechanical_health = 95
                self.health.electrical_health = 95
                self.health.thermal_health = 95
                self.health.vibration_health = 95
                self._change_state(DeviceState.IDLE)  # 维护后先进入待机
    
    def _change_state(self, new_state: DeviceState):
        """改变设备状态"""
        old_state = self.state
        old_start = self.state_start_time  # Capture before update
        self.state = new_state
        self.state_start_time = time.time()

        self.state_history.append({
            'from': old_state.value,
            'to': new_state.value,
            'time': datetime.now().isoformat(),
            'duration': time.time() - old_start  # Use old start time
        })
        
        # 保留最近100条状态历史
        if len(self.state_history) > 100:
            self.state_history = self.state_history[-100:]
        
        self.stats['last_state_change'] = datetime.now().isoformat()
        
        if new_state == DeviceState.MAINTENANCE:
            self.stats['maintenance_count'] += 1
        
        logger.info(f"[行为模拟] 设备 {self.device_name} 状态变更: {old_state.name} → {new_state.name}")
    
    def _update_stats(self, dt: float):
        """更新统计信息"""
        if self.state == DeviceState.RUNNING:
            self.stats['operating_hours'] += dt / 3600.0
            self.stats['total_cycles'] += 1
    
    def _generate_output_data(self) -> Dict[str, Any]:
        """
        生成输出数据
        
        优化：只生成该设备拥有的参数，避免不相关的数据污染
        例如：锅炉设备不会生成振动数据，水质设备不会生成注射压力
        
        重要：STOPPED状态的工作设备不产生数据（已关闭）
        """
        # ===== 停机设备：传感器保持环境值，机械归零 =====
        if self.state == DeviceState.STOPPED and not self._is_monitoring_device:
            # 传感器漂移到环境值（工厂停机后温度回到室温、压力回到大气压等）
            ambient_temp = 25.0 + random.gauss(0, 0.5)
            ambient_pressure = 0.101 + random.gauss(0, 0.001)  # 大气压 MPa
            stopped_params = {
                '_device_state': self.state.name,
                '_health_score': round(self.health.overall_score, 1),
                '_active_fault': self.active_fault.value,
                '_fault_severity': round(self.fault_severity, 2),
                '_active_faults': {f.value: round(s, 2) for f, s in self.active_faults.items()},
                '_fault_count': len(self.active_faults),
                '_operating_hours': round(self.health.operating_hours, 2),
                '_timestamp': datetime.now().isoformat(),
                '_stopped': True,
                # 传感器环境值
                'temperature': round(ambient_temp, 2),
                'boiler_temperature': round(ambient_temp, 2),
                'heat_exchanger_temperature': round(ambient_temp, 2),
                'flue_gas_temperature': round(ambient_temp, 2),
                'mold_temperature': round(ambient_temp, 2),
                'dryer_temperature': round(ambient_temp, 2),
                'oven_temperature': round(ambient_temp, 2),
                'distill_temperature': round(ambient_temp, 2),
                'ambient_temperature': round(ambient_temp, 2),
                'pressure': round(ambient_pressure, 4),
                'boiler_pressure': round(ambient_pressure, 4),
                'injection_pressure': 0.0,
                'hydraulic_pressure': 0.0,
                'pneumatic_pressure': round(ambient_pressure, 4),
                'line_pressure': round(ambient_pressure, 4),
                'level': round(0.5 + random.gauss(0, 0.1), 2),
                'tank_level': round(0.5 + random.gauss(0, 0.1), 2),
                'ph': round(7.0 + random.gauss(0, 0.1), 2),
                'conductivity': round(0.0, 1),
                'turbidity': round(0.0, 1),
                'dissolved_oxygen': round(8.0 + random.gauss(0, 0.2), 2),
                'voltage_a': 0.0, 'voltage_b': 0.0, 'voltage_c': 0.0,
                'current_a': 0.0, 'current_b': 0.0, 'current_c': 0.0,
                'active_power': 0.0, 'reactive_power': 0.0, 'apparent_power': 0.0,
                'power_factor': 0.0, 'frequency': 50.0 + random.gauss(0, 0.01),
                'energy_consumption': round(self._total_energy_kwh, 2),
                'motor_speed': 0.0, 'conveyor_speed': 0.0,
                'pump_speed': 0.0, 'fan_speed': 0.0,
                'vibration': round(0.0 + random.gauss(0, 0.01), 3),
                'vibration_x': round(0.0 + random.gauss(0, 0.01), 3),
                'vibration_y': round(0.0 + random.gauss(0, 0.01), 3),
                'vibration_z': round(0.0 + random.gauss(0, 0.01), 3),
                'spindle_speed': 0.0, 'feed_rate': 0.0,
                'shot_count': round(self._shot_count, 0),
                'cycle_time': 0.0, 'injection_speed': 0.0,
                'injection_force': 0.0, 'clamping_force': 0.0,
                'green_light': 0, 'yellow_light': 1, 'red_light': 0,
                'blue_light': 0, 'white_light': 0, 'buzzer': 0,
                'running_status': 0, 'status': 0, 'boiler_status': 0,
                'packing_status': 0, 'line_status': 0,
                'humidity': round(50.0 + random.gauss(0, 2), 1),
                # 涂装车间（inovance_h5u_01）
                'spray_pressure': round(ambient_pressure, 4),
                'spray_speed': 0.0,
                'coating_thickness': 0.0,
                'painted_count': round(self._shot_count, 0),
                # 水处理（schneider_m340_01）
                'ph_value': round(7.0 + random.gauss(0, 0.1), 2),
                'pump_speed': 0.0,
                'inlet_flow': 0.0, 'outlet_flow': 0.0,
                'water_level': round(0.5 + random.gauss(0, 0.1), 2),
                # 电力分析仪（abb_m4m_01）
                'thd_voltage': round(random.gauss(0, 0.1), 2),
                'electricity_consumption': round(self._total_energy_kwh, 2),
                # IO-Link传感器（turck_iolink_01）— 监测类，停机仍读数
                'iol_temperature_1': round(ambient_temp, 2),
                'iol_pressure_1': round(ambient_pressure, 4),
                'iol_pressure_2': round(ambient_pressure, 4),
                'iol_flow_1': 0.0,
                'iol_distance_1': round(0.0 + random.gauss(0, 0.5), 1),
                'iol_vacuum': round(random.gauss(0, 10), 1),
                # 蒸馏/干燥（hollysys_lk_01）
                'distill_pressure': round(101.3 + random.gauss(0, 0.5), 2),
                'dryer_humidity': round(50.0 + random.gauss(0, 2), 1),
                'reflux_ratio': 0.0,
                # 注塑机（mitsubishi_fx5u_01）
                'cushion_position': 0.0,
                'barrel_temperature': round(ambient_temp, 2),
                # 包装线（delta_dvp_01）
                'label_count': round(self._shot_count, 0),
                'palletizing_count': round(self._shot_count * 0.9, 0),
                'reject_count': round(self._shot_count * 0.01, 0),
                'sealing_temperature': round(ambient_temp, 2),
                # 继电器（relay_output_01）
                'relay_1': 0, 'relay_2': 0, 'relay_3': 0,
                'relay_4': 0, 'relay_5': 0, 'relay_6': 0,
                # MES指标
                'oee': 0.0, 'quality_rate': 0.0, 'defect_rate': 0.0,
                'planned_quantity': 0, 'actual_quantity': 0,
                # OPC UA
                'motor_speed': 0.0,
                # 通用
                'co2': round(400 + random.gauss(0, 10), 0),
            }
            # 过滤：只返回该设备实际拥有的参数
            return {k: v for k, v in stopped_params.items()
                    if k.startswith('_') or k in self._device_param_names}
        
        t = time.time() - self.start_time
        
        # 状态影响因子
        state_temp_factor = 1.0
        state_pressure_factor = 1.0
        state_flow_factor = 1.0
        if self.state == DeviceState.IDLE:
            state_temp_factor = 0.6
            state_pressure_factor = 0.5
            state_flow_factor = 0.1
        elif self.state == DeviceState.FAULT:
            state_flow_factor = 0.3
        
        # ===== 三相电功率物理关联 V*I*cos(phi) =====
        state_factor = 1.0 if self.state == DeviceState.RUNNING else 0.1

        # 三相电压 - 基于负载的平衡系统
        voltage_base = 220 + 5 * math.sin(t / 60)
        voltage_imbalance = random.gauss(0, 0.5)
        voltage_a = voltage_base + voltage_imbalance + random.gauss(0, 1)
        voltage_b = voltage_base + voltage_imbalance * 0.5 + random.gauss(0, 1)
        voltage_c = voltage_base + voltage_imbalance * 0.3 + random.gauss(0, 1)

        # 功率因数 - 随负载变化
        power_factor = 0.85 + 0.1 * math.sin(t / 120) + random.gauss(0, 0.02)
        power_factor = max(0.6, min(0.98, power_factor))

        # 电流 - 由功率和电压推导
        load_power = self._current_power_kw * 1000  # W
        if voltage_a > 0:
            current_base = load_power / (3 * voltage_a * power_factor)
        else:
            current_base = 0

        current_a = current_base * state_factor + random.gauss(0, 0.5)
        current_b = current_base * state_factor * (1 + random.gauss(0, 0.02)) + random.gauss(0, 0.5)
        current_c = current_base * state_factor * (1 + random.gauss(0, 0.02)) + random.gauss(0, 0.5)

        # 有功功率 - 由V*I*cos(phi)计算
        active_power = (voltage_a * current_a + voltage_b * current_b + voltage_c * current_c) * power_factor / 1000

        # 无功功率
        reactive_power = active_power * math.tan(math.acos(min(0.999, power_factor)))

        # 视在功率
        apparent_power = math.sqrt(active_power**2 + reactive_power**2)

        # 生成所有可能的参数值
        all_params = {
            # 温度相关
            'temperature': round(self._current_temp, 2),
            'boiler_temperature': round(self._current_temp, 2),
            'heat_exchanger_temperature': round(self._current_temp * 0.9, 2),
            'flue_gas_temperature': round(self._current_temp * 1.2, 2),
            'mold_temperature': round(self._current_temp * 0.8, 2),
            'dryer_temperature': round(self._current_temp * 0.7, 2),
            'oven_temperature': round(self._current_temp * 1.1, 2),
            'distill_temperature': round(self._current_temp * 0.95, 2),
            'barrel_temperature': round(self._current_temp * 0.85, 2),
            'bearing_temperature': round(self._current_temp * 0.6, 2),
            'water_temperature': round(self._current_temp * 0.5, 2),
            'ambient_temperature': round(self._current_temp * 0.4, 2),
            'sealing_temperature': round(self._current_temp * 0.75, 2),
            'iol_temperature_1': round(self._current_temp * 0.45, 2),
            
            # 压力相关
            'pressure': round(self._current_pressure, 4),
            'boiler_pressure': round(self._current_pressure, 4),
            'distill_pressure': round(self._current_pressure * 100, 2),  # kPa
            'injection_pressure': round(self._current_pressure * 10, 2),  # MPa
            'spray_pressure': round(self._current_pressure * 0.5, 4),
            'system_pressure': round(self._current_pressure, 4),
            'iol_pressure_1': round(self._current_pressure * 0.8, 4),
            'iol_pressure_2': round(self._current_pressure * 1.2, 4),
            'iol_vacuum': round(-80000 + self._current_pressure * 10000, 2),
            
            # 流量相关
            'flow': round(self._current_flow, 2),
            'steam_flow': round(self._current_flow * 0.3, 2),
            'inlet_flow': round(self._current_flow * 1.1, 2),
            'outlet_flow': round(self._current_flow * 0.9, 2),
            'product_flow': round(self._current_flow * 0.5, 2),
            'iol_flow_1': round(self._current_flow * 0.7, 2),
            
            # 液位相关
            'level': round(self._current_level / 100 * 3, 2),  # 转换为米
            'water_level': round(self._current_level / 100 * 3, 2),
            'feed_water_level': round(self._current_level * 30, 2),  # mm
            
            # 电气参数 - 三相电功率物理关联 V*I*cos(phi)
            'voltage_a': round(voltage_a, 1),
            'voltage_b': round(voltage_b, 1),
            'voltage_c': round(voltage_c, 1),
            'current_a': round(max(0, current_a), 2),
            'current_b': round(max(0, current_b), 2),
            'current_c': round(max(0, current_c), 2),
            'active_power': round(max(0, active_power), 2),
            'reactive_power': round(max(0, reactive_power), 2),
            'apparent_power': round(max(0, apparent_power), 2),
            'frequency': round(50 + 0.3 * math.sin(t / 30) + random.gauss(0, 0.05), 2),
            'power_factor': round(power_factor, 3),
            'thd_voltage': round(3.5 + 2.0 * math.sin(t / 120) + random.gauss(0, 0.1), 2),
            
            # 电机参数 - 与物理过程关联
            'speed': round(self._motor_speed, 2),
            'motor_speed': round(self._motor_speed, 2),
            'motor_current': round(max(0, self._motor_current), 2),
            'torque': round(self._motor_current * 0.9 + random.gauss(0, 0.3), 2),
            'vibration_x': round(self._vibration + random.gauss(0, 0.1), 2),
            'vibration_y': round(self._vibration + random.gauss(0, 0.1), 2),
            'vibration_z': round(self._vibration + random.gauss(0, 0.1), 2),
            'clamping_force': round(1500 + 500 * math.sin(t / 30) + random.gauss(0, 20), 2),
            
            # 生产参数
            'cycle_time': round(4.5 + 1.0 * math.sin(t / 120) + random.gauss(0, 0.1), 2),
            'shot_count': int(500 + t * 0.1),
            'label_count': int(800 + t * 0.15),
            'palletizing_count': int(200 + t * 0.05),
            'reject_count': int(5 + t * 0.002),
            'painted_count': int(300 + t * 0.08),
            'batch_count': int(15 + t * 0.001),
            'packaging_speed': int(120 + 20 * math.sin(t / 60) + random.gauss(0, 2)),
            'conveyor_speed': round(12 + 5 * math.sin(t / 60) + random.gauss(0, 0.3), 2),
            'spray_speed': round(8 + 3 * math.sin(t / 45) + random.gauss(0, 0.2), 2),
            'injection_speed': round(80 + 30 * math.sin(t / 30) + random.gauss(0, 1), 2),
            
            # 环境参数
            'humidity': round(62 + 10 * math.sin(t / 600) + random.gauss(0, 1.5), 2),
            'dryer_humidity': round(40 + 15 * math.sin(t / 300) + random.gauss(0, 2), 2),
            'co2': round(520 + 150 * math.sin(t / 120) + random.gauss(0, 10), 2),
            'ph': round(7.0 + 0.5 * math.sin(t / 300) + random.gauss(0, 0.05), 2),
            'ph_value': round(7.0 + 0.5 * math.sin(t / 300) + random.gauss(0, 0.05), 2),
            'oxygen': round(20.9 + 0.5 * math.sin(t / 600) + random.gauss(0, 0.05), 2),
            'oxygen_content': round(5.5 + 2.0 * math.sin(t / 120) + random.gauss(0, 0.2), 2),
            'dissolved_oxygen': round(8.0 + 2.0 * math.sin(t / 120) + random.gauss(0, 0.2), 2),
            'turbidity': round(5 + 3 * math.sin(t / 180) + random.gauss(0, 0.3), 2),
            'conductivity': round(500 + 100 * math.sin(t / 300) + random.gauss(0, 10), 2),
            
            # 能源参数
            'electricity_consumption': round(5000 + t * 0.5, 2),
            'water_consumption': round(500 + t * 0.01, 2),
            'steam_consumption': round(50 + t * 0.01, 2),
            'compressed_air': round(200 + t * 0.005, 2),
            
            # OEE相关
            'oee': round(85 + 5 * math.sin(t / 600) + random.gauss(0, 0.5), 2),
            'quality_rate': round(97 + 2 * math.sin(t / 600) + random.gauss(0, 0.3), 2),
            'defect_rate': round(1.5 + 1.0 * math.sin(t / 300) + random.gauss(0, 0.2), 2),
            'planned_quantity': int(1000 + t * 0.1),
            'actual_quantity': int(970 + t * 0.097),
            
            # 状态
            'boiler_status': self.state.value,
            'packing_status': self.state.value,
            'line_status': self.state.value,
            'running_status': self.state.value,
            
            # 信号灯状态
            'red_light': 1 if self.state == DeviceState.FAULT else 0,
            'yellow_light': 1 if self.state == DeviceState.IDLE else 0,
            'green_light': 1 if self.state == DeviceState.RUNNING else 0,
            'blue_light': 1 if self.state == DeviceState.SETUP else 0,
            'white_light': 1 if self.state == DeviceState.MAINTENANCE else 0,
            'buzzer': 1 if self.state == DeviceState.FAULT else 0,
            
            # 继电器状态（尊重写入值）
            'relay_1': self._written_coils.get(0, 1 if self.state == DeviceState.FAULT else 0),
            'relay_2': self._written_coils.get(1, 1 if self.state == DeviceState.FAULT else 0),
            'relay_3': self._written_coils.get(2, 0),
            'relay_4': self._written_coils.get(3, 0),
            'relay_5': self._written_coils.get(4, 0),
            'relay_6': self._written_coils.get(5, 0),
            
            # 数字输入
            'di_1': 0,  # 急停按钮
            'di_2': 1,  # 门禁开关
            'di_3': 0,  # 烟感报警
            'di_4': 0,  # 水浸报警
            'di_5': 1,  # 手动/自动
            'di_6': 0,
            
            # 特殊参数
            'reflux_ratio': round(3.5 + 0.5 * math.sin(t / 120) + random.gauss(0, 0.05), 2),
            'coating_thickness': round(80 + 20 * math.sin(t / 60) + random.gauss(0, 2), 2),
            'cushion_position': round(50 + 20 * math.sin(t / 30) + random.gauss(0, 0.5), 2),
            'flash_interval': 500,
            'buzzer_volume': 80,
            
            # 元数据
            '_device_state': self.state.name,
            '_health_score': round(self.health.overall_score, 1),
            '_active_fault': self.active_fault.value,
            '_fault_severity': round(self.fault_severity, 2),
            '_active_faults': {f.value: round(s, 2) for f, s in self.active_faults.items()},
            '_fault_count': len(self.active_faults),
            '_operating_hours': round(self.health.operating_hours, 2),
            '_timestamp': datetime.now().isoformat()
        }
        
        # ===== 过滤：只返回该设备拥有的参数 + 元数据 =====
        if self._device_param_names:
            filtered = {}
            for key, value in all_params.items():
                # 元数据始终保留
                if key.startswith('_'):
                    filtered[key] = value
                # 只保留该设备配置中声明的参数
                elif key in self._device_param_names:
                    filtered[key] = value
            return filtered
        
        return all_params
    
    def get_status(self) -> Dict[str, Any]:
        """获取设备状态（含所有活跃故障）"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'state': self.state.name,
            'state_value': self.state.value,
            'health_score': round(self.health.overall_score, 1),
            'active_fault': self.active_fault.value,
            'fault_severity': round(self.fault_severity, 2),
            'active_faults': {f.value: round(s, 2) for f, s in self.active_faults.items()},
            'fault_count': len(self.active_faults),
            'operating_hours': round(self.health.operating_hours, 2),
            'stats': self.stats.copy()
        }
    
    def inject_fault(self, fault_type: FaultType, severity: float = 0.5):
        """手动注入故障（用于测试）"""
        self._trigger_fault(fault_type, severity)
    
    def force_state(self, state: DeviceState):
        """强制设置状态（用于测试）"""
        self._change_state(state)

    def set_recipe(self, recipe_name: str) -> bool:
        """设置当前配方"""
        from 采集层.recipe_simulator import RecipeSimulator
        recipe = RecipeSimulator.RECIPES.get(recipe_name)
        if recipe:
            self.recipe_simulator = RecipeSimulator(recipe, self)
            self.recipe_simulator.start()
            logger.info(f"[行为模拟] 设备 {self.device_name} 已启动配方: {recipe.name}")
            return True
        logger.warning(f"[行为模拟] 配方 '{recipe_name}' 不存在")
        return False

    def stop_recipe(self):
        """停止当前配方"""
        if hasattr(self, 'recipe_simulator') and self.recipe_simulator:
            self.recipe_simulator.stop()
            logger.info(f"[行为模拟] 设备 {self.device_name} 配方已停止")

    def get_recipe_status(self) -> Optional[dict]:
        """获取配方状态"""
        if hasattr(self, 'recipe_simulator') and self.recipe_simulator:
            return self.recipe_simulator.get_status()
        return None


class MultiDeviceSimulator:
    """
    多设备模拟器管理器
    
    管理多个设备行为模拟器，支持：
    - 动态添加/删除设备
    - 统一更新循环
    - 数据聚合
    """
    
    def __init__(self):
        self.simulators: Dict[str, DeviceBehaviorSimulator] = {}
        self._running = False
        self._update_thread: Optional[threading.Thread] = None
        self._update_interval = 1.0  # 更新间隔（秒）
        self._data_callbacks: List[Callable] = []
        self._lock = threading.Lock()
        
        logger.info("[多设备模拟器] 初始化完成")
    
    def add_device(self, device_id: str, device_config: Dict[str, Any]) -> bool:
        """添加设备"""
        with self._lock:
            if device_id in self.simulators:
                logger.warning(f"[多设备模拟器] 设备 {device_id} 已存在")
                return False
            
            simulator = DeviceBehaviorSimulator(device_id, device_config)
            self.simulators[device_id] = simulator
            
            if self._running:
                simulator.start()
            
            logger.info(f"[多设备模拟器] 添加设备: {device_id}")
            return True
    
    def remove_device(self, device_id: str) -> bool:
        """删除设备"""
        with self._lock:
            if device_id not in self.simulators:
                logger.warning(f"[多设备模拟器] 设备 {device_id} 不存在")
                return False
            
            simulator = self.simulators[device_id]
            simulator.stop()
            del self.simulators[device_id]
            
            logger.info(f"[多设备模拟器] 删除设备: {device_id}")
            return True
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调"""
        self._data_callbacks.append(callback)
    
    def start(self):
        """启动所有模拟器"""
        self._running = True
        
        with self._lock:
            for simulator in self.simulators.values():
                simulator.start()
        
        # 启动更新线程
        self._update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self._update_thread.start()
        
        logger.info(f"[多设备模拟器] 已启动，共 {len(self.simulators)} 个设备")
    
    def stop(self):
        """停止所有模拟器"""
        self._running = False
        
        with self._lock:
            for simulator in self.simulators.values():
                simulator.stop()
        
        logger.info("[多设备模拟器] 已停止")
    
    def _update_loop(self):
        """更新循环"""
        while self._running:
            try:
                self._update_all()
                time.sleep(self._update_interval)
            except Exception as e:
                logger.error(f"[多设备模拟器] 更新异常: {e}")
                time.sleep(1)
    
    def _update_all(self):
        """更新所有设备"""
        with self._lock:
            for device_id, simulator in self.simulators.items():
                try:
                    data = simulator.update(self._update_interval)
                    if data:
                        # 触发回调
                        for callback in self._data_callbacks:
                            try:
                                callback(device_id, data)
                            except Exception as e:
                                logger.error(f"[多设备模拟器] 回调异常: {e}")
                except Exception as e:
                    logger.error(f"[多设备模拟器] 设备 {device_id} 更新异常: {e}")
    
    def get_device_status(self, device_id: str) -> Optional[Dict[str, Any]]:
        """获取设备状态"""
        with self._lock:
            simulator = self.simulators.get(device_id)
            if simulator:
                return simulator.get_status()
            return None
    
    def get_all_status(self) -> List[Dict[str, Any]]:
        """获取所有设备状态"""
        with self._lock:
            return [s.get_status() for s in self.simulators.values()]
    
    def inject_fault(self, device_id: str, fault_type: FaultType, severity: float = 0.5):
        """注入故障"""
        with self._lock:
            simulator = self.simulators.get(device_id)
            if simulator:
                simulator.inject_fault(fault_type, severity)
    
    def get_device_data(self, device_id: str) -> Optional[Dict[str, Any]]:
        """获取设备当前数据"""
        with self._lock:
            simulator = self.simulators.get(device_id)
            if simulator:
                return simulator._generate_output_data()
            return None
