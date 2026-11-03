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
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field

from core.safe_numeric import try_finite_float

logger = logging.getLogger(__name__)


# ============================================================
# 配置驱动派生：允许 YAML 声明某个点位由哪个基变量派生
#
# 背景（round 340）：原来「参数名 → 数值」是一张 90 项的硬编码表，
# 只有表里出现过的名字才有数据，系数（如 mold_temperature = temp*0.8）
# 也写死在代码里。结果是：
#   · 新设备只要参数名不在表里 → 该点位永远没有数据（读出来是 None → 25.0 兜底）
#   · 同一参数在不同设备上量纲不同（MPa / kPa / Pa）→ 只能靠再加一条硬编码
#   · 新设备类型（压缩机/冷却塔/AGV…）必须改代码才能有像样的数值
#
# 现在：点位配置里可写 sim 段，优先于内置表。
#   sim:
#     source: temperature    # 派生源（见 _SIM_SOURCES）
#     gain: 0.8              # 乘数
#     offset: -5             # 偏移
#     noise: 0.3             # 高斯噪声标准差
#     min: 0 / max: 300      # 钳位
#     decimals: 2            # 保留小数位；dtype: int 时取整
# ============================================================

_SIM_SOURCES = frozenset({
    'temperature', 'pressure', 'flow', 'level', 'speed', 'vibration',
    'current', 'voltage', 'power', 'power_factor', 'ph', 'humidity', 'turbidity',
    # `ratio` = 负荷率 0~1。工业模拟里极常用（负荷率/开度/占空比），
    # 大量点位的量纲都是「基准值 + 量程 × 负荷率」，没有它就得为每个点位
    # 单独编一个基变量名，配置会膨胀。
    'ratio',
    'counter', 'status', 'status_raw', 'health', 'fault_severity',
    'constant', 'random',
})

# 派生源的别名：内置参数字典里没有「current / voltage / speed / power」这些
# 聚合名，只有 current_a / voltage_a / motor_speed / active_power。
# 若不映射，配置里写 source: current 会取不到值 → **配了等于没配**。
_SIM_SOURCE_ALIASES = {
    'current': ('current_a', 'current_b', 'current_c', 'motor_current'),
    'voltage': ('voltage_a', 'voltage_b', 'voltage_c'),
    'speed': ('motor_speed', 'conveyor_speed', 'pump_speed', 'fan_speed',
              'spindle_speed', 'injection_speed'),
    'power': ('active_power', 'power'),
    'vibration': ('vibration', 'vibration_x'),
    'ph': ('ph', 'ph_value'),
    'level': ('level', 'tank_level', 'water_level'),
    'flow': ('flow', 'inlet_flow', 'outlet_flow'),
    'humidity': ('humidity', 'dryer_humidity'),
}

#: ``simulation.process_model`` 允许覆盖的字段（白名单）。
#:
#: 为什么用白名单而不是 ``setattr`` 任意键：
#: ① 安全 —— 任意键意味着配置能覆盖 ``process_model`` 上的**任何**属性，
#:    包括将来可能加的方法名，等于把内部结构暴露给配置文件；
#: ② 可静态检查 —— 仓库有 ``tests/test_config_keys_consumed.py`` 扫
#:    「配置里声明的键在代码里有没有字面量读取点」。动态 ``setattr`` 让
#:    所有 ``base_*`` 键看起来「没有任何读取点」，闸门直接红。
#:    写成字面量元组后，既通过闸门又自我文档化。
_PROCESS_MODEL_FIELDS = (
    'base_temperature', 'base_pressure', 'base_flow', 'base_level',
    'base_power_kw', 'base_current', 'base_speed', 'base_voltage',
    'temp_pressure_coeff', 'temp_flow_coeff', 'pressure_flow_coeff',
    'thermal_time_constant', 'pressure_time_constant', 'flow_time_constant',
)


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
    base_voltage: float = 380.0             # 基础线电压(V)
    # 为什么单独给电压：电气柜/变压器/逆变器的点位以电压为主，
    # 而内置的 `voltage_a` 是硬编码 220V 相电压 —— 低压柜是 400V 线电压、
    # 光伏逆变器是 800V 直流侧，量纲差一倍以上，不覆盖就读不出合理值。


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

    # ===== 物理极限常量（硬约束） =====
    TEMP_MAX = 500.0          # °C - 最高温度（安全阈值）
    TEMP_MIN = -20.0          # °C - 最低温度
    PRESSURE_MAX = 10.0       # MPa - 最高压力
    PRESSURE_MIN = 0.001      # MPa - 最低压力（接近真空）
    FLOW_MAX = 200.0          # m³/h - 最大流量
    FLOW_MIN = 0.0            # m³/h - 最小流量
    VIBRATION_MAX = 20.0      # mm/s - 最大振动
    CURRENT_MAX = 100.0       # A - 最大电流
    SPEED_MAX = 3600.0        # RPM - 最大转速
    POWER_MAX_KW = 100.0      # kW - 最大功率

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

        # ===== 应用模拟参数档（配置/simulation_profiles.yaml）=====
        # 只补设备配置里没写的键，内联 sim 段优先；没有对应档就原样返回。
        try:
            from .simulation_profiles import apply_profile
            apply_profile(device_config)
        except Exception as exc:  # noqa: BLE001 - 模拟档是调试期假设，不该阻断采集
            logger.warning("[行为模拟] %s: 应用模拟参数档失败（已忽略）: %s",
                           device_id, exc)
        
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

        # 温度扰动尖峰（指数衰减，避免累积）
        self._temp_spike = 0.0

        # 累计生产/能源计数器（stopped状态时引用）
        self._total_energy_kwh = 0.0
        self._shot_count = 0.0

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
        #: 外部按**点位名**写入的值（OPC UA / REST / MQTT 用）。
        #: 与 `_written_values`（按寄存器地址）并存 —— 两种协议的写入语义不同，
        #: 合并成一份会互相覆盖。
        self._point_overrides: Dict[str, float] = {}
        self._written_coils: Dict[int, bool] = {}  # address -> bool
        
        # ===== 设备特定参数名集合（用于过滤输出） =====
        self._device_param_names: set = set()
        self._register_address_map: Dict[int, Dict[str, Any]] = {}  # address -> {name, data_type, scale}
        self._build_param_names()
        # ===== 配置驱动的点位派生规则（优先于内置硬编码表） =====
        self._build_sim_specs()
        
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

        # ===== 按本设备的基准量放大物理极限（见方法注释）=====
        self._calibrate_limits()

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

        # ===== 配置覆盖（最高优先级，解锁「设备类型必须改代码」）=====
        self._apply_process_model_overrides()

    def _calibrate_limits(self):
        """按本设备的基准量放大物理极限。

        类常量（``CURRENT_MAX = 100`` / ``POWER_MAX_KW = 100`` /
        ``SPEED_MAX = 3600`` / ``FLOW_MAX = 200``）是**一台通用小设备**的
        上限。真实设备量级差得很远：

        | 设备            | 基准        | 被类常量钳成 |
        |-----------------|-------------|--------------|
        | 离心冷水机      | 350 kW      | 100 kW       |
        | 离心冷水机      | 620 A       | 100 A        |
        | 光伏逆变器      | 100 kW      | 100 kW（临界）|
        | 储能 BMS        | 250 kW      | 100 kW       |
        | 离心机主轴      | 8000 RPM    | 3600 RPM     |

        被钳住的后果不是「报错」，而是**所有大功率设备的电流/功率/转速都
        停在同一个数字上**，而且看起来完全正常（就是一条被压平的直线）。

        修法：实例级覆盖 —— 取 ``max(类常量, 基准量 × 倍数)``。
        小设备仍受类常量保护（异常值不会飙到天上），大设备不再被误钳。
        """
        pm = self.process_model
        # 用实例属性遮蔽类属性：所有使用点（17 处）无需改动
        self.CURRENT_MAX = max(self.CURRENT_MAX, abs(pm.base_current) * 3.0)
        self.POWER_MAX_KW = max(self.POWER_MAX_KW, abs(pm.base_power_kw) * 3.0)
        self.SPEED_MAX = max(self.SPEED_MAX, abs(pm.base_speed) * 2.0)
        self.FLOW_MAX = max(self.FLOW_MAX, abs(pm.base_flow) * 3.0)
        self.PRESSURE_MAX = max(self.PRESSURE_MAX, abs(pm.base_pressure) * 5.0)
        self.TEMP_MAX = max(self.TEMP_MAX, abs(pm.base_temperature) * 2.5 + 50.0)

    def _apply_process_model_overrides(self):
        """用 ``simulation.process_model`` 段覆盖过程模型。

        允许 YAML 直接给出任意 ``ProcessModel`` 字段，例如：

            simulation:
              process_model:
                base_temperature: 155.0
                base_pressure: 1.0
                thermal_time_constant: 180.0
                base_power_kw: 55

        这样新增设备类型（压缩机/冷却塔/AGV/光伏逆变器…）只需改配置，
        不必再往 ``_configure_process_model`` 里加一个关键词分支。

        未知字段只告警不抛异常：一台设备配错不该让采集进程起不来。
        """
        sim_cfg = self.device_config.get('simulation') or {}
        if not isinstance(sim_cfg, dict):
            logger.warning(
                "[行为模拟] %s: simulation 段应为 dict，实际 %s，已忽略",
                self.device_name, type(sim_cfg).__name__)
            return
        cfg = sim_cfg.get('process_model') or {}
        if not isinstance(cfg, dict) or not cfg:
            return

        applied = []
        for key, raw in cfg.items():
            if key not in _PROCESS_MODEL_FIELDS:
                logger.warning(
                    "[行为模拟] %s: simulation.process_model.%s 不在可覆盖字段白名单里"
                    "（可用: %s），已忽略",
                    self.device_name, key, ', '.join(_PROCESS_MODEL_FIELDS))
                continue
            val = try_finite_float(raw)
            if val is None:
                logger.warning(
                    "[行为模拟] %s: simulation.process_model.%s=%r 不是有限数值，已忽略",
                    self.device_name, key, raw)
                continue
            setattr(self.process_model, key, val)
            applied.append(key)

        if applied:
            # 同步「当前值」，否则第一个采集周期仍输出关键词分支的默认值
            self._current_temp = self.process_model.base_temperature
            self._current_pressure = self.process_model.base_pressure
            self._current_flow = self.process_model.base_flow
            self._current_level = self.process_model.base_level
            self._current_power_kw = self.process_model.base_power_kw
            logger.info("[行为模拟] %s: 过程模型按配置覆盖 %d 项 (%s)",
                        self.device_name, len(applied), ', '.join(applied))
    
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

    def _build_sim_specs(self):
        """收集配置中每个点位的 ``sim`` 派生规则。

        扫描 registers / nodes / topics / endpoints 四个点位区，凡是写了
        ``sim:`` 段的，登记到 ``self._sim_specs``，并在生成数据时优先于
        内置的硬编码参数表。

        设计取舍：非法 source 只告警不抛异常 —— 一台设备配错不该让整个
        采集进程起不来；但也不静默忽略（否则「配了没效果」查不到）。
        """
        self._sim_specs: Dict[str, Dict[str, Any]] = {}
        self._sim_order: List[str] = []

        # 第一遍：先收齐所有声明了 sim 的点位名。
        # 允许 source 指向**另一个派生点位**（如
        # chilled_water_temp_out: {source: chilled_water_temp_in, offset: 5}），
        # 这样配置能直接表达「出水 = 进水 + 温差」这类真实耦合，
        # 而不是被迫让每个点位都从同一个基变量硬套系数。
        declared = set()
        for section in ('registers', 'nodes', 'topics', 'endpoints'):
            for point in (self.device_config.get(section) or []):
                if isinstance(point, dict) and isinstance(point.get('sim'), dict):
                    if point.get('name'):
                        declared.add(point['name'])

        for section in ('registers', 'nodes', 'topics', 'endpoints'):
            for point in (self.device_config.get(section) or []):
                if not isinstance(point, dict):
                    continue
                spec = point.get('sim')
                if not isinstance(spec, dict):
                    continue
                name = point.get('name')
                if not name:
                    logger.warning(
                        "[行为模拟] %s: %s 区有点位写了 sim 段但没有 name，已跳过",
                        self.device_name, section)
                    continue

                source = str(spec.get('source', '')).strip().lower()
                if source not in _SIM_SOURCES and source not in declared:
                    logger.warning(
                        "[行为模拟] %s: 点位 %s 的 sim.source=%r 既不是内置源、"
                        "也不是本设备声明过的点位（可用: %s），该点位退回内置表",
                        self.device_name, name, spec.get('source'),
                        ', '.join(sorted(_SIM_SOURCES)))
                    continue

                # 用默认参数把循环变量**显式绑定**到闭包上（B023）：
                # 虽然这里定义后立即调用、行为本身没错，但一旦以后有人
                # 把这个函数存起来延迟调用，就会静默读到下一轮的值。
                def _num(key: str, default, _spec=spec, _name=name):
                    raw = _spec.get(key, default)
                    if raw is None:
                        return default
                    got = try_finite_float(raw)
                    if got is None:
                        logger.warning(
                            "[行为模拟] %s: 点位 %s 的 sim.%s=%r 不是有限数值，用默认 %r",
                            self.device_name, _name, key, raw, default)
                        return default
                    return got

                self._sim_specs[name] = {
                    'source': source,
                    'gain': _num('gain', 1.0),
                    'offset': _num('offset', 0.0),
                    'noise': _num('noise', 0.0),
                    'min': _num('min', None),
                    'max': _num('max', None),
                    'decimals': _num('decimals', None),
                    'value': _num('value', 0.0),   # source=constant 时用
                    'dtype': str(spec.get('dtype', '')).strip().lower(),
                    # source=status 时的状态编码覆盖（状态名 → 点表编码）
                    'map': spec.get('map') if isinstance(spec.get('map'), dict) else None,
                }
                self._sim_order.append(name)
                # 配置声明的点位必然要出现在输出里
                self._device_param_names.add(name)

        if self._sim_specs:
            logger.info(
                "[行为模拟] %s: 配置驱动点位 %d 个 (%s)",
                self.device_name, len(self._sim_specs),
                ', '.join(self._sim_order[:8])
                + ('…' if len(self._sim_order) > 8 else ''))

    def _status_code(self, spec: Dict[str, Any]) -> int:
        """把内部状态映射成**设备点表的编码**。

        为什么需要映射：``DeviceState.value`` 是
        ``STOPPED=0 / IDLE=1 / RUNNING=2 / FAULT=3``，
        而现场点表绝大多数是 ``0=停机 / 1=运行 / 2=故障``。

        直接输出 ``state.value`` 的后果（改造前就是这样）：设备在运行时
        输出 **2**，而点表语义里 2 是**故障** —— 界面上所有正常设备都显示
        「故障」，而真正故障时输出 3，落到点表范围外，被当成非法值丢弃。
        这是「模拟器自己造出错误数据」里最难发现的一类：**数字合法、语义反了**。

        可用 ``sim.map`` 覆盖，例如三档以上的状态字：
            map: {RUNNING: 3, IDLE: 1, STOPPED: 0, FAULT: 9}
        """
        mapping = spec.get('map')
        if isinstance(mapping, dict):
            hit = mapping.get(self.state.name)
            if hit is not None:
                got = try_finite_float(hit)
                if got is not None:
                    return int(got)

        if self.state in (DeviceState.RUNNING, DeviceState.SETUP):
            return 1
        if self.state == DeviceState.FAULT:
            return 2
        return 0

    def _derive_configured_params(self, base: Dict[str, Any]) -> Dict[str, Any]:
        """按 ``sim`` 规则从基变量派生点位值。

        Args:
            base: 当前周期已算出的参数字典（内置表的结果），作为派生源池。

        Returns:
            仅含配置声明点位的字典；源不可用时不输出该键
            （**不补 0** —— 补 0 会让「没配好」看起来像「设备真读到 0」）。
        """
        if not self._sim_specs:
            return {}

        out: Dict[str, Any] = {}

        # 状态/健康等元信息源
        special = {
            # status_raw 给的是内部枚举原值；对外一律走 status（映射成点表编码）
            'status_raw': float(self.state.value),
            'health': float(self.health.overall_score),
            'fault_severity': float(self.fault_severity),
            'counter': float(self._shot_count),
        }

        for name in self._sim_order:
            spec = self._sim_specs[name]
            source = spec['source']

            if source == 'constant':
                value = spec['value']
            elif source == 'random':
                lo = spec['min'] if spec['min'] is not None else 0.0
                hi = spec['max'] if spec['max'] is not None else 100.0
                if hi < lo:
                    lo, hi = hi, lo
                value = random.uniform(lo, hi)
                # random 源直接用，不再走 gain/offset（否则量纲会二次变形）
                out[name] = self._finalize_sim(name, value, spec, skip_transform=True)
                continue
            elif source == 'status':
                # 状态位要映射成点表编码（0=停机/1=运行/2=故障），
                # 不能直接输出 state.value（那是 0/1/2/3 的内部枚举）
                out[name] = self._status_code(spec)
                continue
            elif source in special:
                value = special[source]
            else:
                raw = base.get(source)
                if raw is None:
                    raw = out.get(source)
                if raw is None:
                    # 聚合名 → 具体列名（current → current_a 等）
                    for alias in _SIM_SOURCE_ALIASES.get(source, ()):
                        raw = base.get(alias)
                        if raw is not None:
                            break
                if raw is None:
                    logger.debug(
                        "[行为模拟] %s: 点位 %s 的派生源 %s 本周期不可用，跳过",
                        self.device_name, name, source)
                    continue
                got = try_finite_float(raw)
                if got is None:
                    continue
                value = got

            out[name] = self._finalize_sim(name, value, spec)

        return out

    @staticmethod
    def _finalize_sim(name: str, value: float, spec: Dict[str, Any],
                      skip_transform: bool = False) -> Any:
        """套用 gain/offset/noise/钳位/取整。"""
        if not skip_transform:
            value = value * spec['gain'] + spec['offset']
        if spec['noise']:
            value += random.gauss(0, spec['noise'])
        if spec['min'] is not None and value < spec['min']:
            value = spec['min']
        if spec['max'] is not None and value > spec['max']:
            value = spec['max']
        if spec['dtype'] == 'int':
            return int(round(value))
        if spec['decimals'] is not None:
            return round(value, int(spec['decimals']))
        return round(value, 3)

    def _apply_simulation_params(self):
        """从设备配置中的simulation_params注入参数到过程模型"""
        # simulation_params通常在SimulationInitializer中设置，
        # 但也可以从device_config中直接读取
        sim_params = self.device_config.get('_simulation_params', {})
        if not sim_params:
            return
        
        base_values = sim_params.get('base_values', {})

        # 用base_values配置过程模型的基础参数
        #
        # round 340：原来每个分支都有 `if abs(value - 当前值) > 阈值` 的门槛
        # （温度 20 / 压力 0.1 / 流量 2），本意是「别被噪声级别的差异扰动」，
        # 但 base_values 是**配置值**不是测量值 —— 配置 55°C 而默认 50°C，
        # 差 5 < 20 → **配置被静默忽略**，模拟器一直按 50°C 跑。
        # 配置值应当无条件生效，差异大小与「是否生效」无关。
        for name, value in base_values.items():
            got = try_finite_float(value)
            if got is None:
                logger.warning(
                    "[行为模拟] %s: base_values.%s=%r 不是有限数值，已忽略",
                    self.device_name, name, value)
                continue
            name_lower = name.lower()
            if 'temperature' in name_lower or 'temp' in name_lower:
                # 取第一个温度参数作为基础温度
                self.process_model.base_temperature = got
                self._current_temp = got
            elif 'pressure' in name_lower and 'spray' not in name_lower:
                self.process_model.base_pressure = got
                self._current_pressure = got
            elif 'flow' in name_lower:
                self.process_model.base_flow = got
                self._current_flow = got
            elif 'level' in name_lower:
                # 配置可能给百分比（0-100）或工程量（如 mm）；>100 视为工程量
                level_pct = got / 300 * 100 if got > 100 else got
                self.process_model.base_level = level_pct
                self._current_level = level_pct
    
    def _detect_monitoring_device(self) -> bool:
        """检测设备是否为监测/检测设备（不受停机命令影响）

        优先用配置里的 ``simulation.monitoring`` 显式声明 —— 关键词猜测
        只能覆盖「常见中文/英文叫法」，像「激光测厚仪」「张力计」这类
        名字里不含「监测/传感器」的仪表会被误判成工作设备，停机时被归零。
        """
        sim_cfg = self.device_config.get('simulation') or {}
        if isinstance(sim_cfg, dict) and 'monitoring' in sim_cfg:
            declared = sim_cfg.get('monitoring')
            if isinstance(declared, bool):
                return declared
            logger.warning(
                "[行为模拟] %s: simulation.monitoring=%r 不是布尔值，回退关键词判定",
                self.device_name, declared)

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
    
    def set_point_override(self, name: str, value: Any) -> bool:
        """按点位名设定值（OPC UA / REST / MQTT 的写入入口）。

        语义是**设定值**：写入后一直保持，直到下次写入。
        不做「过一会儿自动回归工况值」—— 那样运维在界面上改了参数，
        几秒后自己变回去，会以为是写入失败。

        返回 False 表示这个点位不在本设备上（调用方应报点位不存在）。
        """
        from core.safe_numeric import try_finite_float

        point = str(name or '').strip()
        if not point:
            return False
        if point not in self._device_param_names:
            logger.warning('[行为模拟] %s 没有点位 %r，拒绝写入（避免写入一个不存在的量）',
                           self.device_name, point)
            return False
        num = try_finite_float(value)
        if num is None:
            logger.warning('[行为模拟] %s 点位 %s 的写入值 %r 不是有限数值',
                           self.device_name, point, value)
            return False
        self._point_overrides[point] = num
        logger.info('[行为模拟] %s 点位 %s 被外部写入 = %s', self.device_name, point, num)
        return True

    def clear_point_override(self, name: str) -> None:
        """清除某个点位的写入值（恢复正常模型输出）。"""
        self._point_overrides.pop(str(name or ''), None)

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

        # 模拟模式自动启动：IDLE状态超过5秒自动转RUNNING
        if self.state == DeviceState.IDLE:
            if not hasattr(self, '_idle_since'):
                self._idle_since = current_time
            elif current_time - self._idle_since > 5:
                self._change_state(DeviceState.RUNNING)
                logger.info(f"[行为模拟] 设备 {self.device_name} 自动启动，状态→RUNNING")
                self._idle_since = None
        else:
            self._idle_since = None

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

        # 6.5 叠加「外部写入」的点位值
        #
        # 为什么需要这个：OPC UA / REST / MQTT 这类协议没有寄存器地址，
        # 写进来的是**点位名 + 工程值**。如果只把它们转成寄存器语义
        # （hub.write_point 找地址），nodes/topics/endpoints 段的点位
        # 永远写不进去 —— 表现为「模拟设备能读不能控」。
        # 这里做最后一步覆盖：写入值优先于模型算出来的值。
        if self._point_overrides:
            for _name, _val in self._point_overrides.items():
                data[_name] = _val

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
        """更新物理参数 — 牛顿冷却、饱和蒸汽压、质量平衡、电机特性。

        根据当前设备状态和物理模型更新所有过程变量：
        - 温度：一阶惯性 + 牛顿冷却负反馈 + 冷却系统响应
        - 压力：基于 Antoine 方程的饱和蒸汽压（20-250°C 有效区间）
        - 流量：Q ∝ sqrt(ΔP) * f(阀门开度)
        - 液位：质量平衡（流入-流出积分）
        - 电机：P = V*I*cos(phi) 关联，转速-扭矩滑差特性
        - 振动：与速度和负载相关
        - 所有值均有硬限幅，防止数值爆炸

        故障效果不在本方法中处理，由 ``_apply_fault_effects`` 统一执行。

        Args:
            dt: 时间增量（秒），由调用方根据实际帧间隔传入。

        Returns:
            None（就地更新实例属性 ``_current_temp``、``_current_pressure``
            等）。

        Side Effects:
            - 更新 ``self._current_temp``、``self._current_pressure``、
              ``self._current_flow``、``self._current_level`` 等物理参数。
            - 更新 ``self._current_power_kw``、``self._motor_current``、
              ``self._motor_speed``、``self._vibration`` 等电机参数。

        Exceptions:
            不会主动抛出异常。数值计算使用 ``max()``/``min()`` 钳位，
            防止物理量越界。
        """
        t = time.time() - self.start_time

        # 班次影响（24小时周期）
        hour_of_day = (t / 3600) % 24
        shift_factor = 1.0 + 0.05 * math.sin((hour_of_day - 6) * math.pi / 12)

        # ===== 温度 - 牛顿冷却 + 过程加热 + 冷却系统负反馈 =====
        # 物理模型: dT/dt = heating - cooling - cooling_system
        #   heating = 过程热输入（运行时正弦波动 + 扰动）
        #   cooling = h*(T - T_ambient)  牛顿冷却定律
        #   cooling_system = 主动冷却响应（温度越高冷却越强）
        target_temp = self.process_model.base_temperature * shift_factor
        if self.state == DeviceState.RUNNING:
            target_temp += 20 * math.sin(t / 300)
            # 偶发扰动：指数衰减尖峰（自限幅，不会累积）
            if random.random() < 0.001:
                self._temp_spike = random.uniform(10, 30)
                logger.debug(f"[扰动] {self.device_name} 温度尖峰 +{self._temp_spike:.1f}°C")
            if self._temp_spike > 0.1:
                target_temp += self._temp_spike
                self._temp_spike *= 0.95  # 尖峰指数衰减
        elif self.state == DeviceState.IDLE:
            target_temp = self.process_model.base_temperature - 10
        elif self.state == DeviceState.FAULT:
            pass  # 故障温度偏移由 _apply_fault_effects 处理
        else:
            target_temp = self.process_model.base_temperature - 30

        # 一阶惯性（热惯性时间常数决定温度响应速度）
        alpha_temp = 1 - math.exp(-dt / self.process_model.thermal_time_constant)
        self._current_temp += (target_temp - self._current_temp) * alpha_temp
        self._current_temp += random.gauss(0, 0.3)

        # 牛顿冷却负反馈：超出环境温度越多，散热越快
        ambient = self.process_model.base_temperature - 20
        if self._current_temp > ambient:
            self._current_temp -= 0.01 * (self._current_temp - ambient) * dt

        # 冷却系统负反馈：温度超过阈值时冷却系统介入（一阶响应）
        cooling_threshold = self.process_model.base_temperature + 30
        if self._current_temp > cooling_threshold:
            cooling_power = 0.02 * (self._current_temp - cooling_threshold)
            self._current_temp -= cooling_power * dt

        # 硬限幅
        self._current_temp = max(self.TEMP_MIN, min(self.TEMP_MAX, self._current_temp))

        # ===== 压力 - 基于温度的饱和蒸汽压模型 =====
        # Antoine方程: log10(P) = A - B/(C+T)，T为°C，P为mmHg
        # 工业常用简化: P_sat ≈ 0.00001 * exp(0.0464 * T) (MPa, 20-200°C)
        # 超出范围用线性外推（防止指数爆炸）
        T = self._current_temp
        if 20 <= T <= 200:
            target_pressure = 0.00001 * math.exp(0.0464 * T)
        elif T > 200:
            # 200°C时约0.19 MPa，用线性外推（每°C增加0.008 MPa）
            p_at_200 = 0.00001 * math.exp(0.0464 * 200)
            target_pressure = p_at_200 + 0.008 * (T - 200)
        else:
            target_pressure = 0.101  # 大气压

        # 设备基础压力叠加（锅炉/化工等有独立压力源）
        target_pressure = max(target_pressure, self.process_model.base_pressure * 0.5)
        target_pressure += (self.process_model.base_pressure - target_pressure) * 0.3

        # 故障对压力的影响由 _apply_fault_effects 统一处理

        alpha_pressure = 1 - math.exp(-dt / self.process_model.pressure_time_constant)
        self._current_pressure += (target_pressure - self._current_pressure) * alpha_pressure
        self._current_pressure += random.gauss(0, 0.005)

        # 硬限幅
        self._current_pressure = max(self.PRESSURE_MIN, min(self.PRESSURE_MAX, self._current_pressure))

        # ===== 流量 - Q ∝ sqrt(ΔP) * f(阀门/泵状态) =====
        pressure_factor = math.sqrt(max(0, self._current_pressure - 0.1))
        temp_factor = 1 + 0.01 * (self._current_temp - self.process_model.base_temperature)
        state_flow_factor = 1.0 if self.state == DeviceState.RUNNING else 0.1
        if self.state == DeviceState.STOPPED:
            state_flow_factor = 0.0
        target_flow = self.process_model.base_flow * pressure_factor * temp_factor * state_flow_factor

        alpha_flow = 1 - math.exp(-dt / self.process_model.flow_time_constant)
        self._current_flow += (target_flow - self._current_flow) * alpha_flow
        self._current_flow += random.gauss(0, 0.1)

        # 硬限幅
        self._current_flow = max(self.FLOW_MIN, min(self.FLOW_MAX, self._current_flow))

        # ===== 液位 - 质量平衡（流入-流出积分） =====
        flow_balance = self._current_flow - self.process_model.base_flow
        self._current_level += flow_balance * 0.001 * dt
        self._current_level = max(0, min(100, self._current_level))
        self._current_level += random.gauss(0, 0.1)

        # ===== 累计能源和产量（用于stopped状态显示） =====
        if self.state == DeviceState.RUNNING:
            self._total_energy_kwh += self._current_power_kw * dt / 3600.0
            self._shot_count += dt * 0.1  # 约每10秒一个产品

        # ===== 电机参数关联（V*I*cos(phi) 物理一致性） =====
        if self.state == DeviceState.RUNNING:
            # 负载比 = 当前流量 / 基准流量（反映过程负载）
            load_ratio = self._current_flow / max(0.1, self.process_model.base_flow)
            load_ratio = max(0.1, min(3.0, load_ratio))  # 限幅，防止异常负载比

            # 电机电流 ∝ 负载扭矩 ∝ 流量
            self._motor_current = self.process_model.base_current * load_ratio * \
                                  (1 + random.gauss(0, 0.05))

            # 功率由 V*I*cos(phi) 关联（物理一致性）
            # P = sqrt(3) * V_line * I * cos(phi)，简化为 P ∝ I * load
            self._current_power_kw = self.process_model.base_power_kw * load_ratio * \
                                     (1 + random.gauss(0, 0.03))

            # 电机速度略降（感应电机滑差特性：负载越大转速越低）
            self._motor_speed = self.process_model.base_speed * (1 - 0.03 * (load_ratio - 1))

            # 振动 ∝ 速度 + 负载（物理关联）
            speed_ratio = self._motor_speed / max(1, self.process_model.base_speed)
            self._vibration = 1.5 * speed_ratio + 0.5 * (load_ratio - 1) + \
                              random.gauss(0, 0.2)
        else:
            # 非运行状态：电机参数归零或接近零
            self._current_power_kw = max(0, random.gauss(0, 0.2))
            self._motor_current = max(0, random.gauss(0, 0.1))
            self._motor_speed = 0
            self._vibration = max(0, random.gauss(0, 0.05))

        # 硬限幅电机参数
        self._motor_current = max(0, min(self.CURRENT_MAX, self._motor_current))
        self._motor_speed = max(0, min(self.SPEED_MAX, self._motor_speed))
        self._current_power_kw = max(0, min(self.POWER_MAX_KW, self._current_power_kw))
        self._vibration = max(0, min(self.VIBRATION_MAX, self._vibration))

        # 故障影响由 _apply_fault_effects 统一处理

    def _apply_fault_effects(self):
        """应用所有活跃故障的效果（多故障并发 + 级联扩散）

        设计原则：所有故障效果使用一阶惯性逼近退化目标，
        而非逐tick乘法/加法累加。这确保：
        - 数值不会爆炸
        - 退化是渐进的、有界的
        - 恢复时也能平滑回落
        """
        if not self.active_faults:
            return

        for fault_type, severity in list(self.active_faults.items()):
            if fault_type == FaultType.SENSOR_DRIFT:
                # 传感器漂移：叠加一个慢变偏移量（振幅与严重度成正比）
                t = time.time() - self.start_time
                drift = 5 * severity * math.sin(t / 10)
                # 用一阶惯性逼近偏移目标（不会跳变）
                alpha = 1 - math.exp(-self._dt / 3)  # 3秒时间常数
                self._current_temp += drift * alpha

            elif fault_type == FaultType.OVERHEATING:
                # 过热：温度向退化目标偏移（一阶惯性，有界）
                overheat_target = self.process_model.base_temperature + 80 * severity
                alpha = 1 - math.exp(-self._dt / 10)  # 10秒时间常数
                self._current_temp += (overheat_target - self._current_temp) * alpha * 0.1
                # 硬限幅
                self._current_temp = min(self.TEMP_MAX, self._current_temp)
                self._check_cascade(fault_type, severity)

            elif fault_type == FaultType.PRESSURE_LEAK:
                # 压力泄漏：压力向低压目标偏移（一阶惯性，不会降到零）
                leak_target = self._current_pressure * (1 - 0.4 * severity)
                leak_target = max(self.PRESSURE_MIN, leak_target)
                alpha = 1 - math.exp(-self._dt / 5)  # 5秒时间常数
                self._current_pressure += (leak_target - self._current_pressure) * alpha
                # 泄漏导致流量小幅增加（有界）
                flow_boost_target = self._current_flow * (1 + 0.3 * severity)
                alpha_flow = 1 - math.exp(-self._dt / 5)
                self._current_flow += (flow_boost_target - self._current_flow) * alpha_flow
                # 硬限幅
                self._current_pressure = max(self.PRESSURE_MIN, self._current_pressure)
                self._current_flow = min(self.FLOW_MAX, self._current_flow)
                self._check_cascade(fault_type, severity)

            elif fault_type == FaultType.MOTOR_WEAR:
                # 电机磨损：电流增加、振动增加（一阶惯性逼近目标，有界）
                wear_current_target = self._motor_current * (1 + 0.2 * severity)
                wear_current_target = min(self.CURRENT_MAX, wear_current_target)
                alpha = 1 - math.exp(-self._dt / 10)  # 10秒时间常数
                self._motor_current += (wear_current_target - self._motor_current) * alpha

                wear_vib_target = self._vibration + 3 * severity
                wear_vib_target = min(self.VIBRATION_MAX, wear_vib_target)
                alpha_vib = 1 - math.exp(-self._dt / 8)
                self._vibration += (wear_vib_target - self._vibration) * alpha_vib
                self._check_cascade(fault_type, severity)

            elif fault_type == FaultType.COMMUNICATION:
                pass  # 通信故障不影响物理参数

            elif fault_type == FaultType.POWER_FLUCTUATION:
                # 电源波动：功率围绕基准值小幅振荡（有界）
                voltage_factor = 1 + 0.02 * severity * math.sin(time.time() * 5)
                fluctuation_target = self._current_power_kw * voltage_factor
                fluctuation_target = max(0, min(self.POWER_MAX_KW, fluctuation_target))
                alpha = 1 - math.exp(-self._dt / 2)  # 2秒时间常数
                self._current_power_kw += (fluctuation_target - self._current_power_kw) * alpha

        # 故障严重度随时间增长（缓慢）
        for fault_type in list(self.active_faults.keys()):
            growth_rate = 0.01 if fault_type != FaultType.COMMUNICATION else -0.05
            self.active_faults[fault_type] = min(1.0,
                self.active_faults[fault_type] + growth_rate * self._dt)

            # 通信故障会自行恢复（间歇性）
            if fault_type == FaultType.COMMUNICATION and random.random() < 0.01:
                del self.active_faults[fault_type]
                del self.fault_timers[fault_type]
                logger.info(f"[行为模拟] 设备 {self.device_name} 通信故障自行恢复")

        # 最终硬限幅（防止任何叠加情况超出物理极限）
        self._current_temp = max(self.TEMP_MIN, min(self.TEMP_MAX, self._current_temp))
        self._current_pressure = max(self.PRESSURE_MIN, min(self.PRESSURE_MAX, self._current_pressure))
        self._current_flow = max(self.FLOW_MIN, min(self.FLOW_MAX, self._current_flow))
        self._motor_current = max(0, min(self.CURRENT_MAX, self._motor_current))
        self._vibration = max(0, min(self.VIBRATION_MAX, self._vibration))
        self._current_power_kw = max(0, min(self.POWER_MAX_KW, self._current_power_kw))

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
        """生成当前周期的设备输出数据。

        根据设备状态生成完整的参数字典，包括过程变量（温度、压力、
        流量等）、电气参数（三相电压/电流/功率）、生产参数（计数、
        周期时间等）和元数据（健康评分、故障状态等）。

        优化：只生成该设备配置中声明的参数，避免不相关的数据污染。
        例如：锅炉设备不会生成振动数据，水质设备不会生成注射压力。

        特殊处理：
        - STOPPED 状态的工作设备：传感器漂移到环境值，机械参数归零。
        - 监测设备（传感器/分析仪）：不受停机影响，始终输出数据。

        Args:
            无（使用实例属性 ``self.state``、``self._current_temp`` 等）。

        Returns:
            Dict[str, Any]: 设备参数字典。键为参数名（str），值为数值
            （float/int）或元数据（以 ``_`` 前缀标识）。只包含该设备
            配置中声明的参数 + 元数据字段。

        Side Effects:
            无（纯计算方法，不修改实例状态）。

        Exceptions:
            不会主动抛出异常。所有数值均经过 ``round()`` 和 ``max()``/
            ``min()`` 钳位处理。
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
                # 停机时负荷率为 0 —— 有确定值比「点位消失」好：
                # 前端不会因为少一个点而误以为设备没这个参数。
                'ratio': 0.0,
            }
            # 配置驱动的派生点位（源在 stopped_params 里已是环境值/零值，自动跟随停机）
            stopped_params.update(self._derive_configured_params(stopped_params))
            # 过滤：只返回该设备实际拥有的参数
            return {k: v for k, v in stopped_params.items()
                    if k.startswith('_') or k in self._device_param_names}
        
        t = time.time() - self.start_time

        # 电机速度比（用于关联生产参数）
        speed_ratio = self._motor_speed / max(1, self.process_model.base_speed)
        load_ratio = self._current_flow / max(0.1, self.process_model.base_flow)
        load_ratio = max(0.1, min(3.0, load_ratio))

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

        # 电流 - 由功率和电压推导（物理约束：功率不能超过额定3倍）
        self._current_power_kw = max(0, min(self.process_model.base_power_kw * 3, self._current_power_kw))
        load_power = self._current_power_kw * 1000  # W
        if voltage_a > 10:  # 电压不能太小，防止除零
            current_base = load_power / (3 * voltage_a * power_factor)
        else:
            current_base = 0
        current_base = min(current_base, self.process_model.base_current * 3)  # 电流上限

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
            # 负载率 0~1 —— 工业现场极常用的归一化量（负荷率/开度/占空比）。
            #
            # round 351：之前没有这个基变量，而配置里的 `source: ratio` 全都
            # 静默取不到值（`_SIM_SOURCE_ALIASES` 里没有它），表现为「新增设备
            # 一半点位是空的」。加在 base 而不是特判，是因为它就该和其他基变量
            # 一样参与 gain/offset 变换。
            #
            # 取值：由流量负载比推导，钳到 0~1。不用随机数 —— 随机数会让
            # 「负荷率」和「流量」各走各的，出现「流量 0 但负荷率 80%」。
            'ratio': round(max(0.0, min(1.0, load_ratio)), 4),

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
            'clamping_force': round(1500 * load_ratio + random.gauss(0, 20), 2),
            
            # 生产参数 - 与电机速度和负载关联（物理一致性）
            'cycle_time': round(4.5 / max(0.3, speed_ratio) + random.gauss(0, 0.1), 2),
            'shot_count': int(self._shot_count),
            'label_count': int(self._shot_count * 1.6),
            'palletizing_count': int(self._shot_count * 0.4),
            'reject_count': int(self._shot_count * 0.01),
            'painted_count': int(self._shot_count * 0.6),
            'batch_count': int(self._shot_count * 0.03),
            'packaging_speed': int(120 * speed_ratio + random.gauss(0, 2)),
            'conveyor_speed': round(12 * speed_ratio + random.gauss(0, 0.3), 2),
            'spray_speed': round(8 * speed_ratio + random.gauss(0, 0.2), 2),
            'injection_speed': round(80 * speed_ratio + random.gauss(0, 1), 2),
            
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
            'electricity_consumption': round(5000 + self._total_energy_kwh, 2),
            'water_consumption': round(500 + t * 0.01, 2),
            'steam_consumption': round(50 + t * 0.01, 2),
            'compressed_air': round(200 + t * 0.005, 2),
            
            # OEE相关
            'oee': round(85 + 5 * math.sin(t / 600) + random.gauss(0, 0.5), 2),
            'quality_rate': round(97 + 2 * math.sin(t / 600) + random.gauss(0, 0.3), 2),
            'defect_rate': round(1.5 + 1.0 * math.sin(t / 300) + random.gauss(0, 0.2), 2),
            'planned_quantity': int(1000 + self._shot_count),
            'actual_quantity': int(970 + self._shot_count * 0.97),
            
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
        
        # ===== 配置驱动的派生点位（覆盖同名的内置值，并可新增内置表里没有的名字） =====
        all_params.update(self._derive_configured_params(all_params))

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
